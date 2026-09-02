"""Alerting: silent failure is the one thing this project exists to prevent.

After every cycle we check source health. A source that has been failing for
longer than alert_after_hours gets an alert email delivered into its owner's
own Gmail inbox (via the same messages.import path — no send scope needed).
If the owner's destination itself is broken (OAuth), the alert goes to the
admin instead. Re-alerts repeat every realert_after_hours until recovery.
"""

from __future__ import annotations

import logging
import time

from . import __version__
from .config import AppConfig
from .gmail_dest import GmailDestination
from .state import Database, SourceStatus
from .util import fmt_local

log = logging.getLogger("gmailification.alerts")


def _format_alert(cfg: AppConfig, st: SourceStatus, now: float) -> tuple[str, str]:
    hours = (now - st.failing_since) / 3600 if st.failing_since else 0
    is_dest = st.source_key.endswith("/_destination")
    what = f"Gmail destination for user '{st.user}'" if is_dest else f"source '{st.source_key}'"
    subject = f"[gmailification] {what} has been failing for {hours:.1f} hours"
    lines = [
        f"gmailification has been unable to sync {what}.",
        "",
        f"Failing since: {fmt_local(st.failing_since, cfg.timezone, '%Y-%m-%d %H:%M %Z')}",
        f"Consecutive failures: {st.consecutive_failures}",
        f"Last error: {st.last_error or 'unknown'}",
        "",
    ]
    if is_dest or "re-auth" in (st.last_error or "").lower() or "reauth" in (st.last_error or "").lower():
        lines += [
            "This looks like an OAuth problem. To re-authorize, run on the gmailification host:",
            f"    docker compose run --rm gmailification python -m gmailification.authorize --user {st.user} --manual",
            "and follow the printed instructions (a URL to open in a browser).",
        ]
    else:
        lines += [
            "Mail is NOT being lost — it stays untouched in the source mailbox and",
            "will be picked up automatically once the problem clears. But new mail",
            "for this address is not reaching Gmail until then.",
        ]
    lines += ["", f"-- gmailification v{__version__}"]
    return subject, "\n".join(lines)


def check_and_alert(cfg: AppConfig, db: Database, dests: dict[str, GmailDestination]) -> None:
    now = time.time()
    for st in db.statuses_needing_alert(cfg.alert_after_hours, cfg.realert_after_hours, now):
        subject, body = _format_alert(cfg, st, now)
        delivered = False
        targets: list[GmailDestination] = []
        owner = dests.get(st.user)
        is_dest_alert = st.source_key.endswith("/_destination")
        if owner and not is_dest_alert:
            targets.append(owner)
        admin = cfg.admin
        if admin and (is_dest_alert or (cfg.admin_copy_alerts and admin.name != st.user)):
            targets.append(dests[admin.name])
        for dest in targets:
            try:
                dest.import_alert(subject, body)
                delivered = True
                log.warning("alert delivered to %s: %s", dest.email, subject)
            except Exception as exc:
                log.error("could not deliver alert to %s: %s", dest.email, exc)
        if delivered:
            db.mark_alerted(st.source_key, now)
        else:
            # Nothing reachable — keep shouting in the logs; we'll retry the
            # alert next cycle because last_alert_at was not updated.
            log.critical("UNDELIVERABLE ALERT — %s", subject)
