"""Main entrypoint: long-running poll loop + web interface.

    python -m gmailification [--config PATH] [--once] [--user NAME]
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sqlite3
import sys
import time

from concurrent.futures import Future, ThreadPoolExecutor

from . import __version__
from .alerts import check_and_alert
from .config import ConfigError
from .gmail_dest import ReauthNeeded
from .state import Database
from .sync import run_cycle, sync_source
from .util import setup_logging
from .web import AppState, start_server

log = logging.getLogger("gmailification")


def startup_token_check(app: AppState) -> None:
    cfg, dests = app.snapshot()
    for u in cfg.users:
        try:
            dests[u.name].credentials()
            log.info("user %s: Gmail token OK (%s)", u.name, u.destination.email)
        except ReauthNeeded as exc:
            log.error("user %s: %s — authorize via the web UI or run: "
                      "python -m gmailification.authorize --user %s --manual",
                      u.name, exc, u.name)
        except Exception as exc:
            log.warning("user %s: could not verify token at startup: %s", u.name, exc)


def write_heartbeat(path: str) -> None:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(str(time.time()))
    except OSError as exc:
        log.warning("cannot write heartbeat file %s: %s", path, exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gmailification")
    parser.add_argument("--config", default=os.environ.get("GMAILIFICATION_CONFIG", "/config/config.yaml"))
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    parser.add_argument("--user", default=None, help="limit syncing to one user")
    args = parser.parse_args(argv)

    setup_logging()
    # The Gmail API client (httplib2) creates sockets with no timeout, so a
    # dead connection mid-upload would block its sync thread — and the whole
    # cycle — forever. IMAP already passes explicit timeouts, which override
    # this default.
    socket.setdefaulttimeout(120)
    try:
        app = AppState(args.config)
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        return 2

    try:
        os.makedirs(os.path.dirname(app.cfg.db_path) or ".", exist_ok=True)
        db = Database(app.cfg.db_path)
    except (sqlite3.OperationalError, OSError) as exc:
        log.error(
            "cannot open state database %s: %s — the directory is probably not "
            "writable by this user (uid %d). In Docker this means the /data "
            "volume predates image v0.2.2 and is owned by root: recreate it "
            "with 'docker compose down -v && docker compose up -d --build' "
            "(safe before first successful run; afterwards chown it instead).",
            app.cfg.db_path, exc, os.getuid())
        return 2
    startup_token_check(app)

    server = None
    if not args.once:
        server = start_server(app.cfg.http_bind, app.cfg.http_port, app, db)

    n_sources = sum(len(u.sources) for u in app.cfg.users)
    log.info("gmailification %s starting: %d user(s), %d source(s), polling every %ds",
             __version__, len(app.cfg.users), n_sources, app.cfg.poll_interval_seconds)
    if not app.cfg.users:
        log.warning("no users configured yet — open the web UI on port %d to add one",
                    app.cfg.http_port)

    if args.once:
        cfg, dests = app.snapshot()
        stats = run_cycle(cfg, db, dests, only_user=args.user)
        check_and_alert(cfg, db, dests)
        db.prune_history(cfg.history_days)
        write_heartbeat(cfg.heartbeat_file)
        log.info("cycle done: %d source(s) polled, %d imported, %d failing",
                 len(stats.results), stats.imported, len(stats.failed))
        return 1 if stats.failed else 0

    # Each source polls on its own schedule via a shared worker pool, and the
    # scheduler never blocks on a running poll — so one slow source (e.g. a
    # throttled backlog import taking many minutes) cannot delay the others.
    next_run: dict[str, float] = {}     # source_key -> when it is next due
    in_flight: dict[str, Future] = {}   # source_key -> its running poll
    poll_started: dict[str, float] = {}  # source_key -> when that poll began
    last_maintenance = 0.0
    pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="sync")
    try:
        while True:
            now = time.time()
            cfg, dests = app.snapshot()
            sources = {s.key: s for u in cfg.users if args.user in (None, u.name)
                       for s in u.sources}
            users_by_name = {u.name: u for u in cfg.users}

            # Reap finished polls and schedule their next run.
            for key in [k for k, f in in_flight.items() if f.done()]:
                fut = in_flight.pop(key)
                began = poll_started.pop(key, now)
                try:
                    result = fut.result()
                except Exception as exc:  # sync_source catches its own; be safe
                    log.error("%s: poll crashed: %s", key, exc)
                    result = None
                src = sources.get(key)
                if src is not None:
                    next_run[key] = time.time() + src.poll_interval_seconds
                if result is not None and result.ok and src is not None:
                    u = users_by_name.get(src.user)
                    if u is not None:
                        # Any source reaching the user's Gmail clears the
                        # destination-health pseudo source.
                        db.record_success(u.destination_key, u.name)
                took = time.time() - began
                (log.info if took >= 10 else log.debug)(
                    "%s: poll finished in %.1fs", key, took)

            # Launch every due source that isn't already running. Unknown
            # sources (first loop, or newly configured) are due immediately.
            for key, src in sources.items():
                if key not in in_flight and next_run.get(key, 0.0) <= now:
                    in_flight[key] = pool.submit(sync_source, db, src,
                                                 dests[src.user], src.throttle)
                    poll_started[key] = now

            if now - last_maintenance >= 60:
                check_and_alert(cfg, db, dests)
                db.prune_history(cfg.history_days)
                write_heartbeat(cfg.heartbeat_file)
                for key, began in poll_started.items():
                    if now - began >= app.shared.MAX_POLL_SECONDS:
                        log.warning("%s: poll running for %.0f minutes — stuck?",
                                    key, (now - began) / 60)
                last_maintenance = now

            # Publish fresh copies (not mutations) for lock-free web reads.
            app.shared.polling = dict(poll_started)
            app.shared.next_due = dict(next_run)
            app.shared.last_cycle_at = time.time()

            # Sleep until the next source is due — checking running polls every
            # second, and waking immediately for on-demand poll requests.
            now = time.time()
            upcoming = [next_run.get(k, now) for k in sources if k not in in_flight]
            wait = (min(upcoming) - now) if upcoming else cfg.poll_interval_seconds
            wait = max(0.25, min(wait, 60.0))
            if in_flight:
                wait = min(wait, 1.0)
            if app.shared.force_event.wait(timeout=wait):
                forced_user = app.shared.take_poll_request()
                log.info("on-demand poll requested (user=%s)", forced_user or "all")
                for key, src in sources.items():
                    if forced_user in (None, src.user):
                        next_run[key] = 0.0
    except KeyboardInterrupt:
        log.info("shutting down")
        return 0
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        if server is not None:
            server.shutdown()


if __name__ == "__main__":
    sys.exit(main())
