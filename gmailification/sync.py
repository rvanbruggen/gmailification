"""Sync orchestration: poll every source, import new mail, track state.

Each source runs in its own worker thread per cycle, so a slow or dead source
never blocks the others (sockets have hard timeouts). Strict tenant isolation
is structural: a SourceConfig carries its owning user, and the only Gmail
destination a worker ever touches is dests[source.user].
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .config import AppConfig, FolderConfig, SourceConfig, ThrottleConfig
from .gmail_dest import GmailDestination, ReauthNeeded
from .imap_source import ImapSource
from .state import Database
from .util import TransientError, dedupe_key, retry

log = logging.getLogger("gmailification.sync")


@dataclass
class SourceCycleResult:
    source_key: str
    ok: bool
    imported: int = 0
    skipped_dupes: int = 0
    skipped_oversize: int = 0
    deleted: int = 0
    error: str = ""


@dataclass
class CycleStats:
    results: list[SourceCycleResult] = field(default_factory=list)

    @property
    def imported(self) -> int:
        return sum(r.imported for r in self.results)

    @property
    def failed(self) -> list[SourceCycleResult]:
        return [r for r in self.results if not r.ok]


def _throttle_pause(throttle: ThrottleConfig, transferred_bytes: int) -> None:
    """Sleep after handling a message so a big batch can't saturate the line."""
    pause = throttle.message_pause_seconds
    if throttle.bandwidth_limit_kbps > 0:
        pause = max(pause, transferred_bytes / (throttle.bandwidth_limit_kbps * 1024))
    if pause > 0:
        time.sleep(pause)


def _sync_folder(
    db: Database, source: SourceConfig, dest: GmailDestination, imap: ImapSource,
    fcfg: FolderConfig, throttle: ThrottleConfig, budget: int | None
) -> tuple[int, int, int, int, int]:
    """Returns (imported, dupes, oversize, processed, deleted)."""
    folder = fcfg.name
    label = fcfg.label or source.label
    is_inbox = fcfg.place == "inbox"
    imported = dupes = oversize = processed = 0
    delete_mode = source.after_import == "delete"
    to_expunge: list[int] = []
    uidvalidity, uidnext = imap.status(folder)
    state = db.get_folder_state(source.key, folder)

    imap.select(folder, readonly=not delete_mode)

    if state is None:
        # First time we see this folder: start from "now" (optionally backfill
        # a window) instead of importing years of history.
        uids = imap.uids_since(source.backfill_days) if source.backfill_days > 0 else []
        log.info("%s %s: first run, uidvalidity=%d, backfilling %d message(s)",
                 source.key, folder, uidvalidity, len(uids))
    elif state.uidvalidity != uidvalidity:
        # Mailbox was rebuilt server-side; UIDs are meaningless now. Rescan the
        # backfill window (or nothing) and rely on the dedupe table.
        log.warning("%s %s: UIDVALIDITY changed %d -> %d, rescanning",
                    source.key, folder, state.uidvalidity, uidvalidity)
        uids = imap.uids_since(source.backfill_days) if source.backfill_days > 0 else imap.uids_after(0)
    else:
        uids = imap.uids_after(state.last_uid)

    for uid in uids:
        if budget is not None and processed >= budget:
            log.info("%s %s: per-cycle message cap reached, %d uid(s) deferred to next cycle",
                     source.key, folder, len(uids) - processed)
            break
        processed += 1
        raw = imap.fetch_raw(uid)
        if raw is None:
            log.warning("%s %s uid %d: oversize message skipped", source.key, folder, uid)
            oversize += 1
        else:
            key = dedupe_key(raw)
            transferred = False
            if db.is_imported(source.user, key):
                dupes += 1
                transferred = True  # already in the destination
            else:
                try:
                    gmail_id = retry(lambda: dest.import_raw(
                        raw, label, inbox=is_inbox, unread=is_inbox,
                        sent=fcfg.place == "sent"), log=log)
                    db.record_import(source.user, key, source.key, gmail_id)
                    imported += 1
                    transferred = True
                except (TransientError, ReauthNeeded):
                    raise
                except Exception as exc:
                    # Permanently rejected by the API (e.g. malformed message):
                    # record it so we don't retry forever, and move on. It is
                    # NOT flagged for deletion — it never reached Gmail.
                    log.error("%s %s uid %d: permanent import failure, skipping: %s",
                              source.key, folder, uid, exc)
                    db.record_import(source.user, key, source.key, None, status="failed_permanent")
            if delete_mode and transferred:
                # Delete only what is confirmed present in the destination.
                imap.mark_deleted(uid)
                to_expunge.append(uid)
            _throttle_pause(throttle, len(raw) if raw else 0)
        # Advance the cursor after every message so a restart never refetches
        # a large batch; the dedupe table covers the tiny import/record gap.
        db.set_folder_state(source.key, folder, uidvalidity, uid)

    if delete_mode:
        # One expunge per folder pass; also sweeps \Deleted leftovers from a
        # previous cycle that crashed between STORE and EXPUNGE.
        imap.expunge(to_expunge)

    if not uids:
        # Keep the cursor pinned to the mailbox's current top even when idle.
        top = max(state.last_uid if state else 0, uidnext - 1) if state else uidnext - 1
        db.set_folder_state(source.key, folder, uidvalidity, top)
    return imported, dupes, oversize, processed, len(to_expunge)


def sync_source(
    db: Database, source: SourceConfig, dest: GmailDestination,
    throttle: ThrottleConfig | None = None,
) -> SourceCycleResult:
    throttle = throttle or ThrottleConfig()
    budget = throttle.max_messages_per_cycle or None
    result = SourceCycleResult(source_key=source.key, ok=True)
    started = time.monotonic()
    try:
        def attempt():
            remaining = budget
            with ImapSource(source) as imap:
                for fcfg in source.folders:
                    i, d, o, processed, deleted = _sync_folder(
                        db, source, dest, imap, fcfg, throttle, remaining)
                    result.imported += i
                    result.skipped_dupes += d
                    result.skipped_oversize += o
                    result.deleted += deleted
                    if remaining is not None:
                        remaining = max(0, remaining - processed)
        retry(attempt, log=log)
        db.record_success(source.key, source.user)
        db.record_poll(source.key, source.user, ok=True, imported=result.imported,
                       dupes=result.skipped_dupes, deleted=result.deleted,
                       duration=time.monotonic() - started)
        if result.imported or result.deleted:
            log.info("%s: imported %d message(s)%s%s", source.key, result.imported,
                     f", {result.skipped_dupes} duplicate(s) skipped" if result.skipped_dupes else "",
                     f", {result.deleted} moved (deleted from source)" if result.deleted else "")
    except ReauthNeeded as exc:
        result.ok = False
        result.error = str(exc)
        db.record_failure(source.key, source.user, result.error)
        # Also mark the destination itself unhealthy — every source of this
        # user is blocked on the same token.
        db.record_failure(f"{source.user}/_destination", source.user, result.error)
        log.error("%s: %s", source.key, exc)
    except Exception as exc:
        result.ok = False
        result.error = f"{type(exc).__name__}: {exc}"
        db.record_failure(source.key, source.user, result.error)
        log.error("%s: sync failed: %s", source.key, result.error)
    if not result.ok:
        db.record_poll(source.key, source.user, ok=False, imported=result.imported,
                       dupes=result.skipped_dupes, deleted=result.deleted,
                       duration=time.monotonic() - started, error=result.error)
    return result


def run_cycle(
    cfg: AppConfig,
    db: Database,
    dests: dict[str, GmailDestination],
    only_user: str | None = None,
) -> CycleStats:
    tasks: list[SourceConfig] = [
        s for u in cfg.users if only_user in (None, u.name) for s in u.sources
    ]
    stats = CycleStats()
    if not tasks:
        return stats
    with ThreadPoolExecutor(max_workers=min(8, len(tasks)), thread_name_prefix="sync") as pool:
        futures = [pool.submit(sync_source, db, s, dests[s.user], s.throttle) for s in tasks]
        stats.results = [f.result() for f in futures]
    # A successful pass for a user (any source reached their Gmail) clears the
    # destination-health pseudo source.
    for u in cfg.users:
        if any(r.ok for r in stats.results if r.source_key.startswith(u.name + "/")):
            db.record_success(u.destination_key, u.name)
    return stats
