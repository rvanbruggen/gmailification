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

from . import __version__
from .alerts import check_and_alert
from .config import ConfigError
from .gmail_dest import ReauthNeeded
from .state import Database
from .sync import run_cycle
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

    only_user = args.user
    forced = False
    next_run: dict[str, float] = {}  # source_key -> when it is next due
    try:
        while True:
            started = time.time()
            app.shared.cycle_started_at = started
            cfg, dests = app.snapshot()
            candidates = [s for u in cfg.users if only_user in (None, u.name)
                          for s in u.sources]
            # A forced poll (or --once) ignores schedules; otherwise each
            # source runs on its own poll interval. Unknown sources (first
            # loop, or newly configured) are due immediately.
            if forced or args.once:
                due = candidates
            else:
                due = [s for s in candidates if next_run.get(s.key, 0.0) <= started]
            stats = run_cycle(cfg, db, dests, sources=due)
            for s in due:
                next_run[s.key] = time.time() + s.poll_interval_seconds
            check_and_alert(cfg, db, dests)
            db.prune_history(cfg.history_days)
            app.shared.last_cycle_at = time.time()
            app.shared.cycle_started_at = None
            write_heartbeat(cfg.heartbeat_file)
            if due:
                log.info("cycle done in %.1fs: %d source(s) polled, %d imported, %d failing",
                         time.time() - started, len(due), stats.imported, len(stats.failed))
            if args.once:
                return 1 if stats.failed else 0
            # Sleep until the earliest due source — but wake at least every
            # global interval so the heartbeat and healthcheck stay fresh.
            now = time.time()
            upcoming = [next_run.get(s.key, now) for u in cfg.users
                        if args.user in (None, u.name) for s in u.sources]
            wait = min(upcoming) - now if upcoming else cfg.poll_interval_seconds
            wait = max(1.0, min(wait, cfg.poll_interval_seconds))
            if app.shared.force_event.wait(timeout=wait):
                only_user = app.shared.take_poll_request()
                forced = True
                log.info("on-demand poll requested (user=%s)", only_user or "all")
            else:
                only_user = args.user
                forced = False
    except KeyboardInterrupt:
        log.info("shutting down")
        return 0
    finally:
        if server is not None:
            server.shutdown()


if __name__ == "__main__":
    sys.exit(main())
