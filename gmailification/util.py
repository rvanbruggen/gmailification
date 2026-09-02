"""Small shared helpers: dedupe keys, retry with exponential backoff, logging."""

from __future__ import annotations

import email
import email.policy
import hashlib
import json
import logging
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo


def fmt_local(ts: float, tzname: str, fmt: str = "%a %d %b %H:%M %Z") -> str:
    """Render a unix timestamp in the configured display timezone, with the
    zone abbreviation included so the zone is always explicit."""
    try:
        return datetime.fromtimestamp(ts, ZoneInfo(tzname)).strftime(fmt)
    except Exception:
        return datetime.fromtimestamp(ts).strftime(fmt) + " ?"


def dedupe_key(raw: bytes) -> str:
    """Stable identity for a message: its Message-ID, or a content hash if absent.

    The key is scoped per destination user in the database, so the same message
    arriving via two of a user's sources is imported only once, while two users
    each receive their own copy.
    """
    try:
        msg = email.message_from_bytes(raw, policy=email.policy.compat32)
        mid = (msg.get("Message-ID") or "").strip()
    except Exception:
        mid = ""
    if mid:
        return f"mid:{mid.strip('<>').strip()}"
    return "sha256:" + hashlib.sha256(raw).hexdigest()


class TransientError(Exception):
    """An error worth retrying (network hiccup, 429/5xx)."""


def retry(fn, *, attempts: int = 3, base_delay: float = 2.0, log: logging.Logger | None = None):
    """Call fn(); on TransientError retry with exponential backoff (2s, 8s, ...)."""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except TransientError as exc:
            last = exc
            if attempt == attempts - 1:
                break
            delay = base_delay * (4**attempt)
            if log:
                log.warning("transient error (attempt %d/%d), retrying in %.0fs: %s",
                            attempt + 1, attempts, delay, exc)
            time.sleep(delay)
    raise last  # type: ignore[misc]


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging() -> None:
    handler = logging.StreamHandler()
    if os.environ.get("GMAILIFICATION_LOG_JSON", "").lower() in ("1", "true", "yes"):
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    level = os.environ.get("GMAILIFICATION_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=level, handlers=[handler])
    # googleapiclient logs noisy discovery INFO lines
    logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)
