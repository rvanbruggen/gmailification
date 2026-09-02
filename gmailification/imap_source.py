"""Read-only IMAP access to a source mailbox.

Uses stdlib imaplib directly so we control exactly what happens on the wire.
In the default "keep" mode folders are SELECTed read-only and messages are
fetched with BODY.PEEK[], so no flags, no moves, no changes of any kind on the
source — and we get the original raw RFC822 bytes, not a re-serialization.
A source configured with after_import: delete is selected read-write so that
successfully imported messages can be flagged \\Deleted and expunged; fetches
still use BODY.PEEK[] so unrelated flags stay untouched.
"""

from __future__ import annotations

import imaplib
import re
import socket
import ssl
from datetime import datetime, timedelta, timezone

from .config import SourceConfig
from .util import TransientError

_STATUS_RE = {
    "uidvalidity": re.compile(rb"UIDVALIDITY (\d+)"),
    "uidnext": re.compile(rb"UIDNEXT (\d+)"),
}

# Messages larger than this are skipped (Gmail API import limit is 50 MB).
MAX_MESSAGE_BYTES = 45 * 1024 * 1024

# RFC 6154 special-use attributes for the "auto:<use>" folder placeholders.
SPECIAL_USE_ATTRS = {
    "sent": b"\\Sent",
    "archive": b"\\Archive",
    "junk": b"\\Junk",
    "trash": b"\\Trash",
    "drafts": b"\\Drafts",
    "all": b"\\All",
}

_LIST_RE = re.compile(rb'^\((?P<attrs>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+)$')


def parse_list_line(line: bytes) -> tuple[bytes, str] | None:
    """Parse one IMAP LIST response line into (attributes, folder name).

    The name stays in its wire form (modified UTF-7 stays encoded) so it can
    be sent straight back to the server in STATUS/SELECT.
    """
    m = _LIST_RE.match(line.strip())
    if not m:
        return None
    name = m.group("name").strip()
    if name.startswith(b'"') and name.endswith(b'"') and len(name) >= 2:
        name = name[1:-1].replace(b'\\"', b'"').replace(b"\\\\", b"\\")
    return m.group("attrs"), name.decode("ascii", "replace")


def find_special_use(list_lines: list, use: str) -> str | None:
    """Find the folder carrying the special-use attribute for `use`."""
    attr = SPECIAL_USE_ATTRS[use].lower()
    for line in list_lines:
        if not isinstance(line, (bytes, bytearray)):
            continue
        parsed = parse_list_line(bytes(line))
        if parsed is None:
            continue
        attrs, name = parsed
        if attr in (t.lower() for t in attrs.split()):
            return name
    return None


class ImapError(Exception):
    pass


def _quote_folder(folder: str) -> str:
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


def extract_raw_from_fetch(data: list) -> bytes:
    """Pull the literal message body out of an imaplib UID FETCH response."""
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
            return bytes(item[1])
    raise ImapError(f"no message literal in FETCH response: {data!r}")


def parse_search_uids(data: list) -> list[int]:
    uids: list[int] = []
    for item in data:
        if item:
            uids.extend(int(u) for u in item.split())
    return sorted(uids)


def parse_status(line: bytes) -> tuple[int, int]:
    uv = _STATUS_RE["uidvalidity"].search(line)
    un = _STATUS_RE["uidnext"].search(line)
    if not uv or not un:
        raise ImapError(f"cannot parse STATUS response: {line!r}")
    return int(uv.group(1)), int(un.group(1))


class ImapSource:
    """Context manager around one read-only IMAP session."""

    def __init__(self, cfg: SourceConfig, timeout: int = 60):
        self._cfg = cfg
        self._timeout = timeout
        self._client: imaplib.IMAP4_SSL | None = None
        self._resolved: dict[str, str] = {}

    def __enter__(self) -> "ImapSource":
        try:
            self._client = imaplib.IMAP4_SSL(
                self._cfg.host, self._cfg.port, timeout=self._timeout,
                ssl_context=ssl.create_default_context(),
            )
            self._client.login(self._cfg.username, self._cfg.password)
        except (OSError, socket.timeout, ssl.SSLError) as exc:
            raise TransientError(f"IMAP connect to {self._cfg.host}: {exc}") from exc
        except imaplib.IMAP4.error as exc:
            # Login failures are not transient — bad credentials need a human.
            raise ImapError(f"IMAP login for {self._cfg.username}@{self._cfg.host}: {exc}") from exc
        return self

    def __exit__(self, *exc_info) -> None:
        if self._client is not None:
            try:
                self._client.logout()
            except Exception:
                pass
            self._client = None

    def _check(self, typ: str, data: list, what: str) -> list:
        if typ != "OK":
            raise ImapError(f"{what} failed for {self._cfg.key}: {typ} {data!r}")
        return data

    def resolve(self, name: str) -> str:
        """Resolve an "auto:<use>" placeholder to the server's actual folder
        via its SPECIAL-USE attributes; literal names pass through unchanged.
        Cached per connection."""
        if not name.lower().startswith("auto:"):
            return name
        key = name.lower()
        cached = self._resolved.get(key)
        if cached is not None:
            return cached
        use = key.split(":", 1)[1]
        if use not in SPECIAL_USE_ATTRS:
            raise ImapError(f"{self._cfg.key}: unknown auto folder {name!r}")
        try:
            typ, data = self._client.list()
        except (OSError, socket.timeout, imaplib.IMAP4.abort) as exc:
            raise TransientError(f"IMAP LIST: {exc}") from exc
        data = self._check(typ, data, "LIST")
        found = find_special_use(data, use)
        if found is None:
            advertised = []
            for line in data:
                if isinstance(line, (bytes, bytearray)):
                    parsed = parse_list_line(bytes(line))
                    if parsed and any(t.lower() in
                                      {a.lower() for a in SPECIAL_USE_ATTRS.values()}
                                      for t in parsed[0].split()):
                        advertised.append(parsed[1])
            raise ImapError(
                f"{self._cfg.key}: cannot resolve {name!r} — no folder advertises "
                f"{SPECIAL_USE_ATTRS[use].decode()}. "
                + (f"Folders with special-use attributes: {', '.join(advertised)}. "
                   if advertised else "This server may not support SPECIAL-USE. ")
                + "Use the literal folder name instead.")
        self._resolved[key] = found
        return found

    def status(self, folder: str) -> tuple[int, int]:
        """Return (uidvalidity, uidnext) for a folder without selecting it."""
        try:
            typ, data = self._client.status(_quote_folder(folder), "(UIDVALIDITY UIDNEXT)")
        except (OSError, socket.timeout, imaplib.IMAP4.abort) as exc:
            raise TransientError(f"IMAP STATUS {folder}: {exc}") from exc
        data = self._check(typ, data, f"STATUS {folder}")
        return parse_status(data[0])

    def select(self, folder: str, readonly: bool = True) -> None:
        try:
            typ, data = self._client.select(_quote_folder(folder), readonly=readonly)
        except (OSError, socket.timeout, imaplib.IMAP4.abort) as exc:
            raise TransientError(f"IMAP SELECT {folder}: {exc}") from exc
        self._check(typ, data, f"SELECT {folder}")

    def mark_deleted(self, uid: int) -> None:
        try:
            typ, data = self._client.uid("STORE", str(uid), "+FLAGS.SILENT", r"(\Deleted)")
        except (OSError, socket.timeout, imaplib.IMAP4.abort) as exc:
            raise TransientError(f"IMAP STORE uid {uid}: {exc}") from exc
        self._check(typ, data, f"UID STORE {uid} +FLAGS \\Deleted")

    def expunge(self, uids: list[int]) -> None:
        """Expunge flagged messages: UID EXPUNGE (UIDPLUS) when possible.

        Falls back to plain EXPUNGE, which removes every \\Deleted message in
        the folder — acceptable for a folder being drained on purpose.
        """
        try:
            if uids and "UIDPLUS" in getattr(self._client, "capabilities", ()):
                uid_set = ",".join(str(u) for u in uids)
                typ, data = self._client.uid("EXPUNGE", uid_set)
            else:
                typ, data = self._client.expunge()
        except (OSError, socket.timeout, imaplib.IMAP4.abort) as exc:
            raise TransientError(f"IMAP EXPUNGE: {exc}") from exc
        self._check(typ, data, "EXPUNGE")

    def uids_after(self, last_uid: int) -> list[int]:
        """UIDs strictly greater than last_uid in the selected folder."""
        try:
            typ, data = self._client.uid("SEARCH", None, f"UID {last_uid + 1}:*")
        except (OSError, socket.timeout, imaplib.IMAP4.abort) as exc:
            raise TransientError(f"IMAP SEARCH: {exc}") from exc
        data = self._check(typ, data, "UID SEARCH")
        # IMAP quirk: "UID n:*" matches the highest-UID message even when its
        # UID is below n, so filter client-side.
        return [u for u in parse_search_uids(data) if u > last_uid]

    def uids_since(self, days: int) -> list[int]:
        """UIDs of messages received in the last `days` days (backfill)."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")
        try:
            typ, data = self._client.uid("SEARCH", None, "SINCE", since)
        except (OSError, socket.timeout, imaplib.IMAP4.abort) as exc:
            raise TransientError(f"IMAP SEARCH SINCE: {exc}") from exc
        return parse_search_uids(self._check(typ, data, "UID SEARCH SINCE"))

    def fetch_raw(self, uid: int) -> bytes | None:
        """Fetch the raw message; returns None if it exceeds MAX_MESSAGE_BYTES."""
        try:
            typ, data = self._client.uid("FETCH", str(uid), "(RFC822.SIZE)")
            data = self._check(typ, data, f"UID FETCH {uid} SIZE")
            m = re.search(rb"RFC822\.SIZE (\d+)", data[0] if isinstance(data[0], bytes) else data[0][0])
            if m and int(m.group(1)) > MAX_MESSAGE_BYTES:
                return None
            typ, data = self._client.uid("FETCH", str(uid), "(BODY.PEEK[])")
        except (OSError, socket.timeout, imaplib.IMAP4.abort) as exc:
            raise TransientError(f"IMAP FETCH uid {uid}: {exc}") from exc
        data = self._check(typ, data, f"UID FETCH {uid}")
        return extract_raw_from_fetch(data)
