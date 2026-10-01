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
from .state import MAX_RETRY_ATTEMPTS, Database
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


# How often a move-mode folder is checked for leftovers below the cursor.
SWEEP_INTERVAL_SECONDS = 3600


def _sync_folder(
    db: Database, source: SourceConfig, dest: GmailDestination, imap: ImapSource,
    fcfg: FolderConfig, throttle: ThrottleConfig, budget: int | None
) -> tuple[int, int, int, int, int]:
    """Returns (imported, dupes, oversize, processed, deleted)."""
    # "auto:<use>" placeholders resolve to the server's real folder; state is
    # keyed by the resolved name, so switching a config between the literal
    # name and its auto: form keeps the same cursor.
    folder = imap.resolve(fcfg.name)
    label = fcfg.label or source.label
    is_inbox = fcfg.place == "inbox"
    imported = dupes = oversize = processed = deleted = 0
    delete_mode = source.after_import == "delete"
    uidvalidity, uidnext = imap.status(folder)
    state = db.get_folder_state(source.key, folder)

    imap.select(folder, readonly=not delete_mode)
    db.drop_stale_retries(source.key, folder, uidvalidity)

    def handle(uid: int, retrying: bool) -> None:
        """Fetch one message, import it, and (move mode) flag it for deletion.
        Non-transient import failures go to the retry queue."""
        nonlocal imported, dupes, oversize, deleted
        raw = imap.fetch_raw(uid)
        if raw is None:
            log.warning("%s %s uid %d: oversize message skipped", source.key, folder, uid)
            oversize += 1
            if retrying:  # nothing a retry can fix; park it so sweeps skip it
                db.record_retry_failure(source.key, folder, uidvalidity, uid,
                                        "message too large for the Gmail API", give_up=True)
            return
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
                # Rejected by the API or a client-side bug: record it, queue
                # it for later retries, and move on. It is NOT flagged for
                # deletion — it never reached Gmail.
                db.record_import(source.user, key, source.key, None, status="failed_permanent")
                attempts = db.record_retry_failure(source.key, folder, uidvalidity, uid,
                                                   f"{type(exc).__name__}: {exc}")
                log.error("%s %s uid %d: import failed (attempt %d), %s: %s",
                          source.key, folder, uid, attempts,
                          "giving up" if attempts >= MAX_RETRY_ATTEMPTS else "will retry later", exc)
        if transferred:
            db.clear_retry(source.key, folder, uid)
            if delete_mode:
                # Delete only what is confirmed present in the destination,
                # and expunge right away: a connection lost mid-batch (e.g. a
                # nightly router reboot) would otherwise strand flagged
                # messages behind the cursor — Gmail forgets an unexpunged
                # \Deleted flag along with the session.
                imap.mark_deleted(uid)
                imap.expunge([uid])
                deleted += 1
        _throttle_pause(throttle, len(raw))

    def within_budget(pending: int) -> bool:
        if budget is not None and processed >= budget:
            log.info("%s %s: per-cycle message cap reached, %d uid(s) deferred to next cycle",
                     source.key, folder, pending)
            return False
        return True

    if (delete_mode and state is not None and state.uidvalidity == uidvalidity
            and time.time() - (state.last_sweep_at or 0) >= SWEEP_INTERVAL_SECONDS):
        # In move mode everything still in the folder below the cursor is a
        # leftover: a failed import, or a message flagged \Deleted whose
        # EXPUNGE was lost to a dropped connection. Only messages that
        # arrived since we started watching count — older mail predates the
        # source and is left alone. SINCE has day granularity, so the
        # first-run cursor is the exact lower bound; folders from before
        # v0.8 derive it once from arrival times.
        since = state.watch_since or time.time()
        floor = state.watch_from_uid
        if floor is None:
            candidates = imap.uids_received_since(since, 1, state.last_uid)
            dates = imap.internal_dates(candidates)
            floor = max((u for u in candidates if dates.get(u, since) < since), default=0)
            if candidates and not any(u <= floor for u in candidates):
                floor = min(candidates) - 1
            db.set_watch_from_uid(source.key, folder, floor)
            log.info("%s %s: watching since uid %d", source.key, folder, floor)
        leftovers = imap.uids_received_since(since, floor + 1, state.last_uid)
        for uid in leftovers:
            db.queue_retry(source.key, folder, uidvalidity, uid, "left behind in source")
        if leftovers:
            log.info("%s %s: sweep found %d message(s) left in the source",
                     source.key, folder, len(leftovers))
        db.mark_swept(source.key, folder)

    # Retry previously failed messages first (they are older than new mail).
    due = db.due_retries(source.key, folder)
    if due:
        present = set(imap.existing_uids(due))
        for uid in due:
            if uid not in present:  # deleted or moved by the user meanwhile
                db.clear_retry(source.key, folder, uid)
        for i, uid in enumerate(u for u in due if u in present):
            if not within_budget(len(present) - i):
                break
            processed += 1
            handle(uid, retrying=True)

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

    for i, uid in enumerate(uids):
        if not within_budget(len(uids) - i):
            break
        processed += 1
        handle(uid, retrying=False)
        # Advance the cursor after every message so a restart never refetches
        # a large batch; the dedupe table covers the tiny import/record gap,
        # and the retry queue covers messages that failed.
        db.set_folder_state(source.key, folder, uidvalidity, uid)

    if delete_mode and not deleted:
        # Idle pass: a plain EXPUNGE sweeps \Deleted leftovers from a
        # previous cycle that crashed between STORE and EXPUNGE.
        imap.expunge([])

    if not uids:
        # Keep the cursor pinned to the mailbox's current top even when idle.
        top = max(state.last_uid if state else 0, uidnext - 1) if state else uidnext - 1
        db.set_folder_state(source.key, folder, uidvalidity, top)
    return imported, dupes, oversize, processed, deleted


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
    sources: list[SourceConfig] | None = None,
) -> CycleStats:
    """Sync the given sources (or, by default, every source of every user,
    optionally filtered to one user). The scheduler passes `sources` so each
    source can run on its own poll interval."""
    tasks: list[SourceConfig] = sources if sources is not None else [
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
