"""SQLite state store.

Tracks per-source/per-folder IMAP position (UIDVALIDITY + last seen UID),
a per-user Message-ID dedupe table so the sync is idempotent, and per-source
health used for alerting. All timestamps are unix epoch seconds (UTC).

Connections are per-thread (sync workers run in a thread pool); WAL mode keeps
concurrent readers/writers safe.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass

_SCHEMA = """
CREATE TABLE IF NOT EXISTS folder_state (
    source_key   TEXT NOT NULL,
    folder       TEXT NOT NULL,
    uidvalidity  INTEGER NOT NULL,
    last_uid     INTEGER NOT NULL DEFAULT 0,
    updated_at   REAL NOT NULL,
    PRIMARY KEY (source_key, folder)
);
CREATE TABLE IF NOT EXISTS imported_messages (
    user        TEXT NOT NULL,
    dedupe_key  TEXT NOT NULL,
    source_key  TEXT NOT NULL,
    gmail_id    TEXT,
    status      TEXT NOT NULL DEFAULT 'imported',
    imported_at REAL NOT NULL,
    PRIMARY KEY (user, dedupe_key)
);
CREATE TABLE IF NOT EXISTS source_status (
    source_key           TEXT PRIMARY KEY,
    user                 TEXT NOT NULL,
    last_success_at      REAL,
    last_failure_at      REAL,
    failing_since        REAL,
    last_error           TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    total_success        INTEGER NOT NULL DEFAULT 0,
    total_failure        INTEGER NOT NULL DEFAULT 0,
    last_alert_at        REAL
);
CREATE TABLE IF NOT EXISTS poll_history (
    source_key TEXT NOT NULL,
    user       TEXT NOT NULL,
    ts         REAL NOT NULL,
    ok         INTEGER NOT NULL,
    imported   INTEGER NOT NULL DEFAULT 0,
    dupes      INTEGER NOT NULL DEFAULT 0,
    deleted    INTEGER NOT NULL DEFAULT 0,
    duration   REAL NOT NULL DEFAULT 0,
    error      TEXT
);
CREATE INDEX IF NOT EXISTS idx_poll_history_source_ts ON poll_history (source_key, ts);
CREATE INDEX IF NOT EXISTS idx_poll_history_ts ON poll_history (ts);
"""


@dataclass(frozen=True)
class FolderState:
    uidvalidity: int
    last_uid: int


@dataclass(frozen=True)
class PollRecord:
    source_key: str
    user: str
    ts: float
    ok: bool
    imported: int
    dupes: int
    deleted: int
    duration: float
    error: str | None


@dataclass(frozen=True)
class SourceStatus:
    source_key: str
    user: str
    last_success_at: float | None
    last_failure_at: float | None
    failing_since: float | None
    last_error: str | None
    consecutive_failures: int
    total_success: int
    total_failure: int
    last_alert_at: float | None


class Database:
    def __init__(self, path: str):
        self._path = path
        self._local = threading.local()
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._path, timeout=30)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    # -- folder state ------------------------------------------------------

    def get_folder_state(self, source_key: str, folder: str) -> FolderState | None:
        row = self._conn().execute(
            "SELECT uidvalidity, last_uid FROM folder_state WHERE source_key=? AND folder=?",
            (source_key, folder),
        ).fetchone()
        if row is None:
            return None
        return FolderState(uidvalidity=row["uidvalidity"], last_uid=row["last_uid"])

    def set_folder_state(self, source_key: str, folder: str, uidvalidity: int, last_uid: int) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO folder_state (source_key, folder, uidvalidity, last_uid, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(source_key, folder)
                   DO UPDATE SET uidvalidity=excluded.uidvalidity,
                                 last_uid=excluded.last_uid,
                                 updated_at=excluded.updated_at""",
                (source_key, folder, uidvalidity, last_uid, time.time()),
            )

    # -- dedupe ------------------------------------------------------------

    def is_imported(self, user: str, dedupe_key: str) -> bool:
        row = self._conn().execute(
            "SELECT 1 FROM imported_messages WHERE user=? AND dedupe_key=?",
            (user, dedupe_key),
        ).fetchone()
        return row is not None

    def record_import(
        self, user: str, dedupe_key: str, source_key: str, gmail_id: str | None, status: str = "imported"
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO imported_messages
                   (user, dedupe_key, source_key, gmail_id, status, imported_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (user, dedupe_key, source_key, gmail_id, status, time.time()),
            )

    # -- source health -----------------------------------------------------

    def _ensure_status_row(self, conn: sqlite3.Connection, source_key: str, user: str) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO source_status (source_key, user) VALUES (?, ?)",
            (source_key, user),
        )

    def record_success(self, source_key: str, user: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._conn() as conn:
            self._ensure_status_row(conn, source_key, user)
            conn.execute(
                """UPDATE source_status
                   SET last_success_at=?, failing_since=NULL, last_error=NULL,
                       consecutive_failures=0, total_success=total_success+1,
                       last_alert_at=NULL
                   WHERE source_key=?""",
                (now, source_key),
            )

    def record_failure(self, source_key: str, user: str, error: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._conn() as conn:
            self._ensure_status_row(conn, source_key, user)
            conn.execute(
                """UPDATE source_status
                   SET last_failure_at=?,
                       failing_since=COALESCE(failing_since, ?),
                       last_error=?,
                       consecutive_failures=consecutive_failures+1,
                       total_failure=total_failure+1
                   WHERE source_key=?""",
                (now, now, error[:2000], source_key),
            )

    def all_statuses(self) -> list[SourceStatus]:
        rows = self._conn().execute("SELECT * FROM source_status").fetchall()
        return [SourceStatus(**dict(r)) for r in rows]

    def statuses_needing_alert(
        self, alert_after_hours: float, realert_after_hours: float, now: float | None = None
    ) -> list[SourceStatus]:
        now = time.time() if now is None else now
        due = []
        for st in self.all_statuses():
            if st.failing_since is None:
                continue
            if now - st.failing_since < alert_after_hours * 3600:
                continue
            if st.last_alert_at is not None and now - st.last_alert_at < realert_after_hours * 3600:
                continue
            due.append(st)
        return due

    def mark_alerted(self, source_key: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._conn() as conn:
            conn.execute("UPDATE source_status SET last_alert_at=? WHERE source_key=?", (now, source_key))

    # -- poll history ------------------------------------------------------

    def record_poll(self, source_key: str, user: str, *, ok: bool, imported: int = 0,
                    dupes: int = 0, deleted: int = 0, duration: float = 0.0,
                    error: str | None = None, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO poll_history
                   (source_key, user, ts, ok, imported, dupes, deleted, duration, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (source_key, user, now, 1 if ok else 0, imported, dupes, deleted,
                 duration, (error or None) and error[:500]),
            )

    @staticmethod
    def _poll_row(r) -> PollRecord:
        return PollRecord(source_key=r["source_key"], user=r["user"], ts=r["ts"],
                          ok=bool(r["ok"]), imported=r["imported"], dupes=r["dupes"],
                          deleted=r["deleted"], duration=r["duration"], error=r["error"])

    def history_since(self, since: float, source_key: str | None = None) -> list[PollRecord]:
        """All poll records after `since`, oldest first."""
        if source_key is None:
            rows = self._conn().execute(
                "SELECT * FROM poll_history WHERE ts >= ? ORDER BY ts", (since,)).fetchall()
        else:
            rows = self._conn().execute(
                "SELECT * FROM poll_history WHERE ts >= ? AND source_key=? ORDER BY ts",
                (since, source_key)).fetchall()
        return [self._poll_row(r) for r in rows]

    def recent_events(self, limit: int = 20, source_key: str | None = None) -> list[PollRecord]:
        """Noteworthy polls (failures, or anything imported/deleted), newest first."""
        query = ("SELECT * FROM poll_history WHERE (ok=0 OR imported>0 OR deleted>0)"
                 + ("" if source_key is None else " AND source_key=?")
                 + " ORDER BY ts DESC LIMIT ?")
        args = (limit,) if source_key is None else (source_key, limit)
        rows = self._conn().execute(query, args).fetchall()
        return [self._poll_row(r) for r in rows]

    def prune_history(self, days: float, now: float | None = None) -> int:
        now = time.time() if now is None else now
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM poll_history WHERE ts < ?", (now - days * 86400,))
            return cur.rowcount
