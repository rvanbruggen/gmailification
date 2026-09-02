"""Gmail API delivery into one destination account.

Delivery uses users.messages.import (NOT insert, NOT SMTP): messages get
standard delivery scanning/classification and thread normally, and nothing
travels over SMTP so there are no SPF/DMARC forwarding penalties.

Scopes are the narrowest pair that covers import + label management:
gmail.insert (add mail only) + gmail.labels (create/list labels). The service
can never read, modify or delete existing mail in the destination account.
"""

from __future__ import annotations

import io
import json
import logging
import os
import threading
from email.message import EmailMessage
from email.utils import formatdate

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

from .util import TransientError

SCOPES = [
    "https://www.googleapis.com/auth/gmail.insert",
    "https://www.googleapis.com/auth/gmail.labels",
]

_RETRIABLE_HTTP = {429, 500, 502, 503, 504}

log = logging.getLogger("gmailification.gmail")


def token_info(token_file: str) -> tuple[str, str]:
    """Offline token inspection for the UI: (state, detail).

    States: "missing", "ok", "refreshable" (expired but auto-refreshable),
    "needs-reauth", "unreadable".
    """
    if not os.path.exists(token_file):
        return "missing", "not authorized yet"
    try:
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    except (ValueError, json.JSONDecodeError) as exc:
        return "unreadable", str(exc)
    if creds.valid:
        return "ok", "authorized"
    if creds.expired and creds.refresh_token:
        return "refreshable", "access token expired; will refresh automatically"
    return "needs-reauth", "token invalid and not refreshable"


class ReauthNeeded(Exception):
    """The stored OAuth token is missing, expired beyond refresh, or revoked."""

    def __init__(self, user: str, detail: str):
        super().__init__(f"user {user}: Gmail OAuth re-authorization needed ({detail})")
        self.user = user


class GmailDestination:
    def __init__(self, user: str, email_addr: str, token_file: str):
        self.user = user
        self.email = email_addr
        self._token_file = token_file
        self._lock = threading.RLock()
        self._creds: Credentials | None = None
        self._label_ids: dict[str, str] = {}
        self._local = threading.local()  # per-thread service (keep-alive HTTP)

    # -- auth --------------------------------------------------------------

    def credentials(self) -> Credentials:
        with self._lock:
            if self._creds is None:
                if not os.path.exists(self._token_file):
                    raise ReauthNeeded(self.user, f"no token file at {self._token_file}")
                try:
                    self._creds = Credentials.from_authorized_user_file(self._token_file, SCOPES)
                except (ValueError, json.JSONDecodeError) as exc:
                    raise ReauthNeeded(self.user, f"unreadable token file: {exc}") from exc
            if not self._creds.valid:
                if self._creds.expired and self._creds.refresh_token:
                    try:
                        self._creds.refresh(Request())
                    except RefreshError as exc:
                        raise ReauthNeeded(self.user, f"token refresh rejected: {exc}") from exc
                    except Exception as exc:
                        raise TransientError(f"token refresh for {self.user}: {exc}") from exc
                    self._save_token()
                else:
                    raise ReauthNeeded(self.user, "token invalid and not refreshable")
            return self._creds

    def _save_token(self) -> None:
        tmp = self._token_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(self._creds.to_json())
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._token_file)

    def _service(self):
        # One service per thread, cached: googleapiclient http objects are not
        # thread-safe, but rebuilding per call cost a fresh TLS handshake for
        # every message. Credentials refresh stays serialized via self._lock
        # (and the cached AuthorizedHttp re-reads the same Credentials object).
        svc = getattr(self._local, "service", None)
        creds = self.credentials()  # keeps the token fresh / persisted
        if svc is None or getattr(self._local, "creds", None) is not creds:
            svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
            self._local.service = svc
            self._local.creds = creds
        return svc

    # -- labels ------------------------------------------------------------

    def label_ids(self, label_path: str) -> list[str]:
        """Resolve a (possibly nested) label path to its ID, creating as needed."""
        with self._lock:
            if label_path in self._label_ids:
                return [self._label_ids[label_path]]
        svc = self._service()
        existing = self._call(lambda: svc.users().labels().list(userId="me").execute())
        by_name = {l["name"]: l["id"] for l in existing.get("labels", [])}
        # Create each ancestor so "Pulled/foo" nests under a real "Pulled".
        parts = label_path.split("/")
        for i in range(1, len(parts) + 1):
            name = "/".join(parts[:i])
            if name in by_name:
                continue
            body = {
                "name": name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            }
            try:
                created = self._call(lambda: svc.users().labels().create(userId="me", body=body).execute())
                by_name[name] = created["id"]
            except HttpError as exc:
                if exc.resp.status == 409:  # created concurrently
                    refreshed = self._call(lambda: svc.users().labels().list(userId="me").execute())
                    by_name = {l["name"]: l["id"] for l in refreshed.get("labels", [])}
                else:
                    raise
        with self._lock:
            self._label_ids.update({n: i for n, i in by_name.items()})
        return [by_name[label_path]]

    # -- delivery ----------------------------------------------------------

    def import_raw(self, raw: bytes, label_path: str, *, inbox: bool = True,
                   unread: bool = True, sent: bool = False,
                   never_mark_spam: bool = False) -> str:
        """Import a raw RFC822 message; returns the Gmail message id.

        System labels control placement: INBOX/UNREAD for received mail,
        SENT to surface a message in Gmail's Sent view, neither for
        archive-only imports (label + All Mail).
        """
        ids = self.label_ids(label_path)
        if inbox:
            ids.append("INBOX")
        if unread:
            ids.append("UNREAD")
        if sent:
            ids.append("SENT")
        svc = self._service()
        media = MediaIoBaseUpload(io.BytesIO(raw), mimetype="message/rfc822", resumable=len(raw) > 4 * 1024 * 1024)
        req = svc.users().messages().import_(
            userId="me",
            internalDateSource="dateHeader",
            neverMarkSpam=never_mark_spam,
            processForCalendar=False,
            body={"labelIds": ids},
            media_body=media,
        )
        result = self._call(req.execute)
        return result["id"]

    def import_alert(self, subject: str, body_text: str, alert_label: str = "Pulled/alerts") -> str:
        """Deliver an operational alert into this user's own inbox.

        Uses the same import path (no send scope needed) with neverMarkSpam so
        alerts cannot be spam-filed.
        """
        msg = EmailMessage()
        msg["From"] = f"gmailification <gmailification-noreply@{self.email.split('@', 1)[1]}>"
        msg["To"] = self.email
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg.set_content(body_text)
        return self.import_raw(msg.as_bytes(), alert_label, never_mark_spam=True)

    # -- error mapping -----------------------------------------------------

    def _call(self, fn):
        try:
            return fn()
        except HttpError as exc:
            status = exc.resp.status if exc.resp is not None else None
            if status in _RETRIABLE_HTTP:
                raise TransientError(f"Gmail API {status} for {self.user}: {exc}") from exc
            if status in (401, 403) and b"invalid_grant" in (exc.content or b""):
                raise ReauthNeeded(self.user, str(exc)) from exc
            raise
        except (ConnectionError, TimeoutError, OSError) as exc:
            raise TransientError(f"Gmail API network error for {self.user}: {exc}") from exc
