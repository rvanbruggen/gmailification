"""Web interface: dashboard, configuration editor, and machine endpoints.

Unauthenticated (LAN):
    GET  /healthz        Docker healthcheck
    GET  /status         JSON per-source health
    POST /poll[?user=x]  trigger an immediate poll

Everything else is the human UI and requires HTTP Basic Auth with the
password from the GMAILIFICATION_ADMIN_PASSWORD environment variable (any
username). If that variable is not set, the dashboard renders read-only and
all configuration routes are disabled — set the variable to enable editing.

Configuration edits are validated with the full config parser before being
written (atomically, with a .bak), and the running service hot-reloads.
Passwords entered here are stored as mode-0600 files under secrets_dir,
never in the YAML. POST requests are rejected when they carry a foreign
Origin header (basic CSRF protection for a LAN tool).
"""

from __future__ import annotations

import base64
import hmac
import html
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__
from .config import AppConfig, ConfigError, format_folder_text, load_config
from .config_store import ConfigStore
from .gmail_dest import GmailDestination, token_info
from .state import Database
from .util import fmt_local

log = logging.getLogger("gmailification.web")

ADMIN_PASSWORD_ENV = "GMAILIFICATION_ADMIN_PASSWORD"


class Shared:
    """State shared between the main loop and the HTTP server."""

    def __init__(self, poll_interval: int):
        self.poll_interval = poll_interval
        self.last_cycle_at: float | None = None   # last scheduler pass
        # Live per-source view, replaced wholesale by the scheduler each pass
        # (never mutated in place) so web threads can read without locking.
        self.polling: dict[str, float] = {}       # source_key -> poll started at
        self.next_due: dict[str, float] = {}      # source_key -> next poll due at
        self.force_event = threading.Event()
        self.force_user: str | None = None
        self._lock = threading.Lock()

    def request_poll(self, user: str | None) -> None:
        with self._lock:
            # Two rapid requests for different users degrade to a full poll.
            self.force_user = user if (user and not self.force_event.is_set()) else None
            self.force_event.set()

    def take_poll_request(self) -> str | None:
        with self._lock:
            user = self.force_user
            self.force_user = None
            self.force_event.clear()
            return user

    # A single poll (e.g. a throttled backlog import) may legitimately run for
    # many minutes without blocking anything else; only a poll stuck beyond
    # this cap makes the app unhealthy.
    MAX_POLL_SECONDS = 2 * 3600

    def healthy(self) -> bool:
        now = time.time()
        polling = self.polling
        if polling and now - min(polling.values()) >= self.MAX_POLL_SECONDS:
            return False  # a poll has been stuck for hours
        if self.last_cycle_at is None:
            return False
        return now - self.last_cycle_at < max(3 * self.poll_interval, 600)


class AppState:
    """Holds the live config + destinations; supports hot reload."""

    def __init__(self, config_path: str):
        self.config_path = config_path
        self._lock = threading.RLock()
        self.cfg: AppConfig = load_config(config_path)
        self.dests: dict[str, GmailDestination] = self._build_dests(self.cfg)
        self.shared = Shared(self.cfg.poll_interval_seconds)
        self.store = ConfigStore(config_path, self.cfg.secrets_dir)

    @staticmethod
    def _build_dests(cfg: AppConfig) -> dict[str, GmailDestination]:
        return {
            u.name: GmailDestination(u.name, u.destination.email, u.destination.token_file)
            for u in cfg.users
        }

    def snapshot(self) -> tuple[AppConfig, dict[str, GmailDestination]]:
        with self._lock:
            return self.cfg, self.dests

    def reload(self) -> None:
        with self._lock:
            cfg = load_config(self.config_path)
            self.cfg = cfg
            self.dests = self._build_dests(cfg)
            self.shared.poll_interval = cfg.poll_interval_seconds
            self.store = ConfigStore(self.config_path, cfg.secrets_dir)
        log.info("configuration reloaded: %d user(s), %d source(s)",
                 len(cfg.users), sum(len(u.sources) for u in cfg.users))


def _esc(value) -> str:
    return html.escape(str(value), quote=True)


_STYLE = """
:root{
  --bg:#f4f6f8; --surface:#fff; --border:#e4e7ec; --ink:#101828; --ink2:#667085;
  --accent:#2563eb; --accent-ink:#fff; --ok:#0ca30c; --bad:#d03b3b; --warn:#b45309;
  --ok-soft:rgba(12,163,12,.11); --bad-soft:rgba(208,59,59,.11);
  --warn-soft:rgba(250,178,25,.16); --muted:rgba(102,112,133,.22);
  --shadow:0 1px 2px rgba(16,24,40,.06),0 1px 3px rgba(16,24,40,.08);
  color-scheme: light dark;
}
@media (prefers-color-scheme: dark){:root{
  --bg:#0d1117; --surface:#161b22; --border:#30363d; --ink:#e6edf3; --ink2:#8b949e;
  --accent:#3b82f6; --warn:#d97706; --muted:rgba(139,148,158,.25); --shadow:none;
}}
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  margin:0;background:var(--bg);color:var(--ink);line-height:1.55}
main{max-width:64rem;margin:0 auto;padding:.8rem 1rem 3rem}
header{background:var(--surface);border-bottom:1px solid var(--border)}
.hwrap{max-width:64rem;margin:0 auto;padding:.6rem 1rem;display:flex;align-items:center;gap:1rem}
.brand{display:flex;align-items:center;gap:.55rem;font-weight:700;font-size:1.05rem;
  color:var(--ink);text-decoration:none;letter-spacing:-.01em}
nav{margin-left:auto;display:flex;align-items:center;gap:1rem}
nav a{color:var(--ink2);text-decoration:none;font-size:.92rem;font-weight:500}
nav a:hover{color:var(--accent)}
.chip{font-size:.72rem;border:1px solid var(--border);border-radius:99px;
  padding:.05rem .55rem;color:var(--ink2)}
h1{font-size:1.3rem;margin:1rem 0 .3rem} h2{font-size:1.02rem;margin:1.5rem 0 .4rem}
a{color:var(--accent)}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;
  padding:1rem 1.2rem;margin:.8rem 0;box-shadow:var(--shadow)}
.muted{color:var(--ink2);font-size:.85rem}
.ok{color:var(--ok)} .bad{color:var(--bad)} .warn{color:var(--warn)}
table{border-collapse:collapse;width:100%;font-size:.92rem}
td,th{text-align:left;padding:.45rem .7rem .45rem 0;border-bottom:1px solid var(--border);
  vertical-align:middle}
th{font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;color:var(--ink2);font-weight:600}
tr:last-child td{border-bottom:none}
.pill{display:inline-flex;align-items:center;gap:.35rem;font-size:.78rem;font-weight:600;
  border-radius:99px;padding:.12rem .6rem;white-space:nowrap}
.pill::before{content:'';width:.42rem;height:.42rem;border-radius:50%;background:currentColor}
.pill.p-ok{color:var(--ok);background:var(--ok-soft)}
.pill.p-bad{color:var(--bad);background:var(--bad-soft)}
.pill.p-warn{color:var(--warn);background:var(--warn-soft)}
.pill.p-muted{color:var(--ink2);background:var(--muted)}
form.inline{display:inline}
label{display:block;margin-top:.7rem;font-size:.88rem;color:var(--ink2);font-weight:500}
input,select,textarea{display:block;margin-top:.25rem;padding:.45rem .6rem;min-width:18rem;
  max-width:100%;font:inherit;color:var(--ink);background:var(--bg);
  border:1px solid var(--border);border-radius:8px}
input:focus,select:focus,textarea:focus{outline:2px solid var(--accent);outline-offset:1px}
button{font:inherit;font-weight:600;margin-top:1rem;padding:.45rem 1.05rem;border-radius:8px;
  cursor:pointer;border:1px solid var(--accent);background:var(--accent);color:var(--accent-ink)}
button:hover{filter:brightness(1.08)}
button.danger{background:transparent;color:var(--bad);border-color:var(--bad)}
.banner{background:var(--warn-soft);border:1px solid var(--warn);border-radius:10px;
  padding:.6rem 1rem;margin:1rem 0;font-size:.9rem}
code{background:var(--muted);padding:.08rem .35rem;border-radius:5px;font-size:.85em}
svg.strip{width:100%;height:26px;display:block;border-radius:4px}
.tick{fill:var(--muted)} .tick.quiet{fill:var(--ok);opacity:.38}
.tick.mail{fill:var(--ok)} .tick.fail{fill:var(--bad)}
.legend{font-size:.75rem;color:var(--ink2);margin-top:.35rem}
.striplabel{font-size:.72rem;color:var(--ink2);margin:.8rem 0 .2rem;
  text-transform:uppercase;letter-spacing:.05em;font-weight:600}
td.stripcell{min-width:11rem;width:34%}
footer{max-width:64rem;margin:0 auto;padding:0 1rem 2rem;color:var(--ink2);font-size:.78rem}
"""

_LOGO = """<svg xmlns="http://www.w3.org/2000/svg" width="26" height="26" viewBox="0 0 32 32" aria-hidden="true">
<defs><linearGradient id="glg" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="#2563eb"/><stop offset="1" stop-color="#0ea5e9"/>
</linearGradient></defs>
<rect width="32" height="32" rx="8" fill="url(#glg)"/>
<path d="M16 4.5 v7.5 M12.4 8.6 L16 12.2 l3.6 -3.6" stroke="#fff" stroke-width="2.4"
 fill="none" stroke-linecap="round" stroke-linejoin="round"/>
<rect x="6.5" y="15.5" width="19" height="12" rx="2.2" fill="#fff" opacity=".94"/>
<path d="M7.5 17.5 L16 23 l8.5 -5.5" stroke="#2563eb" stroke-width="1.9" fill="none"
 stroke-linecap="round" stroke-linejoin="round"/>
</svg>"""


def _page(title: str, body: str) -> bytes:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{_esc(title)}</title>"
        "<link rel='icon' type='image/svg+xml' href='/favicon.svg'>"
        f"<style>{_STYLE}</style></head><body>"
        "<header><div class='hwrap'>"
        f"<a class='brand' href='/'>{_LOGO}<span>gmailification</span></a>"
        "<nav><a href='/'>Dashboard</a><a href='/config'>Settings</a>"
        f"<span class='chip'>v{__version__}</span></nav>"
        "</div></header>"
        f"<main>{body}</main>"
        "<footer>self-hosted mail consolidation &middot; "
        "<a href='https://github.com/rvanbruggen/gmailification'>source</a></footer>"
        "</body></html>"
    ).encode("utf-8")


def _strip_svg(records, start: float, end: float, buckets: int, tzname: str,
               width: int = 480, height: int = 26) -> str:
    """Poll-history timeline: one tick per time bucket.

    State is double-encoded (color + height) so it survives color-vision
    deficiencies: failures are full-height red, buckets with imported mail
    medium green, quiet-ok short faint green, no-data short grey. Every tick
    carries a text tooltip, and pages pair the strip with an events table.
    """
    span = (end - start) / buckets
    agg = [[0, 0, 0] for _ in range(buckets)]  # polls, failures, imported+moved
    for r in records:
        if r.ts < start or r.ts >= end:
            continue
        b = min(buckets - 1, int((r.ts - start) / span))
        agg[b][0] += 1
        agg[b][1] += 0 if r.ok else 1
        agg[b][2] += r.imported + r.deleted
    bar_w = width / buckets
    rects = []
    for i, (polls, fails, moved) in enumerate(agg):
        when = fmt_local(start + i * span, tzname, "%d %b %H:%M %Z")
        if polls == 0:
            h, cls, tip = 6, "nodata", f"{when}: no polls"
        elif fails:
            h, cls, tip = height, "fail", f"{when}: {fails} failed poll(s)"
        elif moved:
            h, cls, tip = int(height * 0.72), "mail", f"{when}: {moved} message(s)"
        else:
            h, cls, tip = 10, "quiet", f"{when}: ok, no new mail"
        rects.append(
            f"<rect class='tick {cls}' x='{i * bar_w:.2f}' y='{height - h}' "
            f"width='{max(bar_w - 1.2, 0.8):.2f}' height='{h}' rx='1.4'>"
            f"<title>{_esc(tip)}</title></rect>")
    return (f"<svg class='strip' viewBox='0 0 {width} {height}' "
            f"preserveAspectRatio='none' role='img' aria-label='poll history'>"
            f"{''.join(rects)}</svg>")


_STRIP_LEGEND = ("<p class='legend'>each tick is a time slice &mdash; "
                 "<span class='bad'>full-height red = failed polls</span> &middot; "
                 "<span class='ok'>tall green = mail imported</span> &middot; "
                 "short faint green = ok, quiet &middot; grey = no data</p>")


def _fmt_ts(ts: float, tzname: str) -> str:
    return fmt_local(ts, tzname, "%a %d %b %H:%M %Z")


def _make_handler(app: AppState, db: Database):
    # In-flight OAuth consent flows, keyed by user name.
    pending_flows: dict[str, object] = {}
    flows_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = f"gmailification/{__version__}"

        # -- plumbing ------------------------------------------------------

        def _send_json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, indent=2).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, code: int, title: str, body: str) -> None:
            data = _page(title, body)
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, location: str) -> None:
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _admin_password(self) -> str:
            return os.environ.get(ADMIN_PASSWORD_ENV, "")

        def _authed(self) -> bool:
            password = self._admin_password()
            if not password:
                return False
            header = self.headers.get("Authorization", "")
            if not header.startswith("Basic "):
                return False
            try:
                decoded = base64.b64decode(header[6:]).decode("utf-8")
                _user, _, given = decoded.partition(":")
            except Exception:
                return False
            return hmac.compare_digest(given, password)

        def _require_auth(self) -> bool:
            """True if the request may proceed. Sends the response otherwise."""
            if not self._admin_password():
                return True  # editing disabled; GET pages render read-only
            if self._authed():
                return True
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="gmailification"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        @property
        def _editable(self) -> bool:
            return bool(self._admin_password())

        def _check_origin(self) -> bool:
            origin = self.headers.get("Origin")
            if not origin:
                return True
            host = self.headers.get("Host", "")
            if urlparse(origin).netloc == host:
                return True
            self._send_html(403, "Forbidden", "<h1>Cross-origin request rejected</h1>")
            return False

        def _form(self) -> dict[str, str]:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8") if length else ""
            return {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}

        def log_message(self, fmt, *args):
            log.debug("http: " + fmt, *args)

        # -- GET -----------------------------------------------------------

        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/favicon.svg", "/favicon.ico"):
                body = _LOGO.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml")
                self.send_header("Cache-Control", "public, max-age=86400")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/healthz":
                ok = app.shared.healthy()
                self._send_json(200 if ok else 503, {
                    "status": "ok" if ok else "stale",
                    "version": __version__,
                    "last_cycle_at": app.shared.last_cycle_at,
                    "polls_running": app.shared.polling,
                })
                return
            if path == "/status":
                self._send_json(200, {"version": __version__, "sources": [
                    {
                        "source": st.source_key,
                        "healthy": st.failing_since is None,
                        "last_success_at": st.last_success_at,
                        "consecutive_failures": st.consecutive_failures,
                        "last_error": st.last_error,
                        "total_success": st.total_success,
                        "total_failure": st.total_failure,
                    }
                    for st in db.all_statuses()
                ]})
                return
            if not self._require_auth():
                return
            if path == "/history":
                params = parse_qs(urlparse(self.path).query)
                hours = min(float((params.get("hours") or ["24"])[0]), 24 * 60)
                source = (params.get("source") or [None])[0]
                since = time.time() - hours * 3600
                self._send_json(200, {"version": __version__, "since": since, "polls": [
                    {"source": r.source_key, "ts": r.ts, "ok": r.ok,
                     "imported": r.imported, "dupes": r.dupes, "deleted": r.deleted,
                     "duration": round(r.duration, 2), "error": r.error}
                    for r in db.history_since(since, source)
                ]})
                return
            parts = [p for p in path.split("/") if p]
            if path == "/":
                self._page_dashboard()
            elif path == "/config":
                self._page_config()
            elif len(parts) == 2 and parts[0] == "users":
                self._page_user(parts[1])
            elif len(parts) == 4 and parts[0] == "users" and parts[2] == "sources":
                self._page_source(parts[1], parts[3])
            else:
                self._send_html(404, "Not found", "<h1>Not found</h1>")

        # -- POST ----------------------------------------------------------

        def do_POST(self):
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/poll":
                user = (parse_qs(parsed.query).get("user") or [None])[0]
                form_user = None
                if self.headers.get("Content-Length"):
                    form_user = self._form().get("user") or None
                app.shared.request_poll(user or form_user)
                if "text/html" in (self.headers.get("Accept") or ""):
                    self._redirect("/")
                else:
                    self._send_json(202, {"status": "poll scheduled",
                                          "user": user or form_user or "all"})
                return
            if not self._require_auth():
                return
            if not self._check_origin():
                return
            if not self._editable:
                self._send_html(403, "Editing disabled", _DISABLED_NOTICE)
                return
            parts = [p for p in path.split("/") if p]
            form = self._form()
            try:
                if path == "/config":
                    self._mutate(lambda raw: app.store.update_globals(raw, form))
                    self._redirect("/config")
                elif path == "/users":
                    self._mutate(lambda raw: app.store.add_user(
                        raw, form.get("name", ""), form.get("email", "")))
                    self._redirect(f"/users/{form.get('name', '').strip()}")
                elif len(parts) == 3 and parts[0] == "users" and parts[2] == "delete":
                    self._mutate(lambda raw: app.store.delete_user(raw, parts[1]))
                    self._redirect("/")
                elif len(parts) == 3 and parts[0] == "users" and parts[2] == "sources":
                    self._mutate(lambda raw: app.store.upsert_source(raw, parts[1], form))
                    self._redirect(f"/users/{parts[1]}")
                elif (len(parts) == 5 and parts[0] == "users" and parts[2] == "sources"
                        and parts[4] == "delete"):
                    self._mutate(lambda raw: app.store.delete_source(raw, parts[1], parts[3]))
                    self._redirect(f"/users/{parts[1]}")
                elif len(parts) == 4 and parts[0] == "users" and parts[2] == "oauth":
                    if parts[3] == "start":
                        self._oauth_start(parts[1])
                    elif parts[3] == "finish":
                        self._oauth_finish(parts[1], form.get("response_url", ""))
                    else:
                        self._send_html(404, "Not found", "<h1>Not found</h1>")
                else:
                    self._send_html(404, "Not found", "<h1>Not found</h1>")
            except ConfigError as exc:
                self._send_html(400, "Configuration error",
                                f"<h1>Configuration error</h1><p class='bad'>{_esc(exc)}</p>"
                                "<p><a href='javascript:history.back()'>&larr; go back</a></p>")

        def _mutate(self, mutation) -> None:
            raw = app.store.read_raw()
            mutation(raw)
            app.store.write_raw(raw)
            app.reload()

        # -- OAuth ---------------------------------------------------------

        def _oauth_start(self, user: str) -> None:
            from google_auth_oauthlib.flow import Flow
            cfg, _ = app.snapshot()
            ucfg = cfg.user(user)
            if not os.path.exists(cfg.oauth_client_file):
                raise ConfigError(f"OAuth client file not found: {cfg.oauth_client_file}")
            from .gmail_dest import SCOPES
            flow = Flow.from_client_secrets_file(
                cfg.oauth_client_file, SCOPES, redirect_uri="http://localhost:8378/")
            auth_url, _state = flow.authorization_url(
                access_type="offline", prompt="consent", login_hint=ucfg.destination.email)
            with flows_lock:
                pending_flows[user] = flow
            self._send_html(200, f"Authorize {user}", f"""
<h1>Authorize Gmail for {_esc(user)}</h1>
<ol>
<li>Open this link — or send it to {_esc(ucfg.destination.email)}'s owner:<br>
    <a href="{_esc(auth_url)}">{_esc(auth_url[:80])}&hellip;</a></li>
<li>Sign in as <b>{_esc(ucfg.destination.email)}</b> and approve. The browser then
    tries to open <code>http://localhost:8378/</code> and shows a
    "can't connect" error — <b>that is expected</b>.</li>
<li>Copy the full URL from the browser's address bar
    (starts with <code>http://localhost:8378/?state=</code>) and paste it below.</li>
</ol>
<form method="post" action="/users/{_esc(user)}/oauth/finish">
  <label>Pasted URL <input name="response_url" required
         placeholder="http://localhost:8378/?state=..."></label>
  <button type="submit">Save token</button>
</form>""")

        def _oauth_finish(self, user: str, response_url: str) -> None:
            with flows_lock:
                flow = pending_flows.pop(user, None)
            if flow is None:
                raise ConfigError("no authorization in progress for this user — start again")
            cfg, _ = app.snapshot()
            ucfg = cfg.user(user)
            os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
            try:
                flow.fetch_token(authorization_response=response_url.strip())
            except Exception as exc:
                raise ConfigError(f"token exchange failed: {exc}") from exc
            token_file = ucfg.destination.token_file
            os.makedirs(os.path.dirname(token_file) or ".", exist_ok=True)
            fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(flow.credentials.to_json())
            app.reload()  # drop any cached (stale) credentials
            self._redirect(f"/users/{user}")

        # -- pages ---------------------------------------------------------

        def _page_dashboard(self) -> None:
            cfg, _ = app.snapshot()
            statuses = {st.source_key: st for st in db.all_statuses()}
            now = time.time()
            day_ago = now - 24 * 3600
            by_source: dict[str, list] = {}
            for rec in db.history_since(day_ago):
                by_source.setdefault(rec.source_key, []).append(rec)
            body = ["<h1>Dashboard</h1>"]
            if not self._editable:
                body.append(_DISABLED_NOTICE)
            last = app.shared.last_cycle_at
            running = len(app.shared.polling)
            body.append(f"<p class='muted'>Scheduler last ran: "
                        f"{fmt_local(last, cfg.timezone, '%Y-%m-%d %H:%M:%S %Z') if last else 'not yet'}"
                        f" &middot; {running} poll(s) running"
                        f" &middot; default interval {cfg.poll_interval_seconds}s</p>")
            if self._editable:
                body.append("<form class='inline' method='post' action='/poll'>"
                            "<button type='submit'>Poll all now</button></form>")
            for u in cfg.users:
                state, detail = token_info(u.destination.token_file)
                tok_pill = "p-ok" if state in ("ok", "refreshable") else "p-bad"
                rows = []
                for s in u.sources:
                    st = statuses.get(s.key)
                    if st is None or (st.last_success_at is None and st.failing_since is None):
                        health = "<span class='pill p-muted'>no data yet</span>"
                    elif st.failing_since is None:
                        health = "<span class='pill p-ok'>healthy</span>"
                    else:
                        health = (f"<span class='pill p-bad' title='{_esc(st.last_error or '')}'>"
                                  f"failing &times;{st.consecutive_failures}</span>")
                    poll_began = app.shared.polling.get(s.key)
                    due = app.shared.next_due.get(s.key)
                    if poll_began is not None:
                        mins = (now - poll_began) / 60
                        activity = ("syncing now" if mins < 1
                                    else f"syncing now &middot; {mins:.0f}m")
                        health += f" <span class='pill p-warn'>{activity}</span>"
                    elif due is not None:
                        wait_s = due - now
                        activity = ("next poll: due now" if wait_s <= 1
                                    else f"next poll in {wait_s:.0f}s" if wait_s < 120
                                    else f"next poll in {wait_s / 60:.0f}m")
                        health += f"<div class='muted' style='font-size:.72rem'>{activity}</div>"
                    mode = "move" if s.after_import == "delete" else "copy"
                    strip = _strip_svg(by_source.get(s.key, []), day_ago, now,
                                       buckets=48, tzname=cfg.timezone)
                    rows.append(
                        f"<tr><td><a href='/users/{_esc(u.name)}/sources/{_esc(s.name)}'>"
                        f"{_esc(s.name)}</a></td><td class='muted'>{_esc(s.username)}</td>"
                        f"<td>{mode}</td><td>{health}</td>"
                        f"<td class='stripcell'>{strip}</td></tr>")
                poll_btn = (f"<form class='inline' method='post' action='/poll?user={_esc(u.name)}'>"
                            f"<button type='submit'>Poll now</button></form>"
                            if self._editable else "")
                body.append(f"""
<div class='card'>
<h2 style='margin-top:.2rem'><a href='/users/{_esc(u.name)}'>{_esc(u.name)}</a>
    <span class='muted'>&rarr; {_esc(u.destination.email)}</span></h2>
<p>Gmail token: <span class='pill {tok_pill}' title='{_esc(detail)}'>{_esc(state)}</span></p>
<table><tr><th>source</th><th>account</th><th>mode</th><th>health</th><th>last 24 h</th></tr>
{''.join(rows) or '<tr><td colspan=5 class="muted">no sources</td></tr>'}</table>
{poll_btn}
</div>""")
            body.append(_STRIP_LEGEND)
            events = db.recent_events(limit=15)
            if events:
                ev_rows = []
                for e in events:
                    if not e.ok:
                        what = (f"<span class='pill p-bad'>failed</span> "
                                f"<span class='muted'>{_esc((e.error or '')[:110])}</span>")
                    else:
                        bits = []
                        if e.imported:
                            bits.append(f"{e.imported} imported")
                        if e.deleted:
                            bits.append(f"{e.deleted} moved")
                        what = f"<span class='pill p-ok'>{_esc(', '.join(bits))}</span>"
                    ev_rows.append(f"<tr><td class='muted'>{_esc(_fmt_ts(e.ts, cfg.timezone))}</td>"
                                   f"<td>{_esc(e.source_key)}</td><td>{what}</td></tr>")
                body.append("<h2>Recent activity</h2><div class='card'><table>"
                            "<tr><th>when</th><th>source</th><th>event</th></tr>"
                            + "".join(ev_rows) + "</table></div>")
            if self._editable:
                body.append("""
<h2>Add user</h2>
<form method='post' action='/users' class='card'>
  <label>Name (short, no spaces) <input name='name' required></label>
  <label>Destination Gmail address <input name='email' type='email' required></label>
  <button type='submit'>Add user</button>
</form>""")
            self._send_html(200, "gmailification", "".join(body))

        def _page_config(self) -> None:
            cfg, _ = app.snapshot()
            if not self._editable:
                self._send_html(200, "Settings", "<h1>Settings</h1>" + _DISABLED_NOTICE)
                return
            t = cfg.throttle
            options = "".join(
                f"<option value='{_esc(u.name)}' {'selected' if u.name == cfg.admin_user else ''}>"
                f"{_esc(u.name)}</option>" for u in cfg.users)
            self._send_html(200, "Settings", f"""
<h1>Global settings</h1>
<form method='post' action='/config' class='card'>
  <label>Poll interval (seconds) <span class='muted'>(default — override per source on its page)</span>
    <input name='poll_interval_seconds' type='number' min='30' value='{cfg.poll_interval_seconds}'></label>
  <label>Alert after (hours failing)
    <input name='alert_after_hours' type='number' step='0.5' value='{cfg.alert_after_hours}'></label>
  <label>Re-alert every (hours)
    <input name='realert_after_hours' type='number' step='0.5' value='{cfg.realert_after_hours}'></label>
  <label>Timezone for displayed times <span class='muted'>(IANA name)</span>
    <input name='timezone' value='{_esc(cfg.timezone)}' placeholder='Europe/Brussels'></label>
  <h2>Throttling <span class='muted'>(defaults — override per source on its page)</span></h2>
  <label>Bandwidth limit (KB/s, 0 = unlimited)
    <input name='bandwidth_limit_kbps' type='number' value='{t.bandwidth_limit_kbps}'></label>
  <label>Max messages per source per cycle (0 = unlimited)
    <input name='max_messages_per_cycle' type='number' value='{t.max_messages_per_cycle}'></label>
  <label>Pause between messages (seconds)
    <input name='message_pause_seconds' type='number' step='0.1' value='{t.message_pause_seconds}'></label>
  <h2>Alert routing</h2>
  <label>Admin user <select name='admin_user'>{options}</select></label>
  <button type='submit'>Save</button>
</form>
<p class='muted'>Saved to <code>{_esc(app.config_path)}</code> after validation
(a <code>.bak</code> of the previous version is kept). Hand-written YAML comments
do not survive a UI edit.</p>""")

        def _page_user(self, name: str) -> None:
            cfg, _ = app.snapshot()
            try:
                u = cfg.user(name)
            except ConfigError:
                self._send_html(404, "Not found", "<h1>No such user</h1>")
                return
            state, detail = token_info(u.destination.token_file)
            tok_pill = "p-ok" if state in ("ok", "refreshable") else "p-bad"
            rows = "".join(
                f"<tr><td><a href='/users/{_esc(name)}/sources/{_esc(s.name)}'>{_esc(s.name)}</a></td>"
                f"<td>{_esc(s.username)}</td><td>{_esc(s.host)}</td>"
                f"<td>{_esc(s.label)}</td>"
                f"<td>{'move' if s.after_import == 'delete' else 'copy'}</td></tr>"
                for s in u.sources)
            editable = self._editable
            oauth_btn = (f"<form class='inline' method='post' action='/users/{_esc(name)}/oauth/start'>"
                         f"<button type='submit'>{'Re-a' if state != 'missing' else 'A'}uthorize Gmail&hellip;</button>"
                         "</form>" if editable else "")
            add_form = f"""
<h2>Add source</h2>
<form method='post' action='/users/{_esc(name)}/sources' class='card'>
  <label>Name (short, no spaces) <input name='name' required></label>
  <label>IMAP host <input name='host' required></label>
  <label>Port <input name='port' type='number' value='993'></label>
  <label>Username <input name='username' required></label>
  <label>Password <input name='password' type='password'>
    <span class='muted'>stored as a 0600 file on the data volume — for Google
    accounts use an app password</span></label>
  <label>&hellip;or environment variable name <input name='password_env'></label>
  <label>Gmail label <input name='label' placeholder='Pulled/&lt;name&gt; (default)'></label>
  <label>Folders (one per line; append <code>:: sent</code> for the sent folder (<code>auto:sent</code> finds it by its IMAP special-use attribute, any language),
    <code>:: archive</code> for label-only)
    <textarea name='folders' rows='3'>INBOX</textarea></label>
  <label>Backfill days on first run <input name='backfill_days' type='number' value='0'></label>
  <label>After import <select name='after_import'>
    <option value='keep'>keep (copy — source untouched)</option>
    <option value='delete'>delete (move — expunge after confirmed import)</option>
  </select></label>
  <div class='striplabel'>overrides (blank = inherit global settings)</div>
  <label>Poll interval (seconds, min 10) <input name='poll_interval_seconds' placeholder='inherit'></label>
  <label>Bandwidth limit (KB/s) <input name='throttle_bandwidth_limit_kbps' placeholder='inherit'></label>
  <label>Max messages per cycle <input name='throttle_max_messages_per_cycle' placeholder='inherit'></label>
  <label>Pause between messages (s) <input name='throttle_message_pause_seconds' placeholder='inherit'></label>
  <button type='submit'>Add source</button>
</form>
<form method='post' action='/users/{_esc(name)}/delete'
      onsubmit="return confirm('Remove user {_esc(name)} and all their sources from the config?')">
  <button type='submit' class='danger'>Remove this user</button>
</form>""" if editable else _DISABLED_NOTICE
            self._send_html(200, f"User {name}", f"""
<h1>{_esc(name)} <span class='muted'>&rarr; {_esc(u.destination.email)}</span></h1>
<p>Gmail token: <span class='pill {tok_pill}' title='{_esc(detail)}'>{_esc(state)}</span>
   {oauth_btn}</p>
<table><tr><th>source</th><th>account</th><th>host</th><th>label</th><th>mode</th></tr>
{rows or '<tr><td colspan=5 class="muted">no sources</td></tr>'}</table>
{add_form}""")

        def _page_source(self, user: str, sname: str) -> None:
            cfg, _ = app.snapshot()
            try:
                u = cfg.user(user)
                s = next(x for x in u.sources if x.name == sname)
            except (ConfigError, StopIteration):
                self._send_html(404, "Not found", "<h1>No such source</h1>")
                return
            now = time.time()
            key = f"{user}/{sname}"
            day = _strip_svg(db.history_since(now - 24 * 3600, key), now - 24 * 3600, now,
                             buckets=96, tzname=cfg.timezone)
            span_days = max(1, int(cfg.history_days))
            long_start = now - span_days * 86400
            longer = _strip_svg(db.history_since(long_start, key), long_start, now,
                                buckets=84, tzname=cfg.timezone)
            events = db.recent_events(limit=30, source_key=key)
            ev_rows = "".join(
                f"<tr><td class='muted'>{_esc(_fmt_ts(e.ts, cfg.timezone))}</td>"
                + ("<td><span class='pill p-bad'>failed</span></td>"
                   f"<td class='muted'>{_esc((e.error or '')[:140])}</td>" if not e.ok else
                   f"<td><span class='pill p-ok'>ok</span></td>"
                   f"<td>{e.imported} imported"
                   + (f", {e.deleted} moved" if e.deleted else "")
                   + (f", {e.dupes} duplicate(s) skipped" if e.dupes else "") + "</td>")
                + "</tr>"
                for e in events)
            history_html = f"""
<div class='card'>
  <div class='striplabel'>last 24 hours</div>{day}
  <div class='striplabel'>last {span_days} days</div>{longer}
  {_STRIP_LEGEND}
</div>
<h2>Recent events</h2>
<div class='card'>
{'<table><tr><th>when</th><th>status</th><th>detail</th></tr>' + ev_rows + '</table>'
 if ev_rows else "<p class='muted'>Nothing noteworthy yet — no failures, no imports.</p>"}
</div>"""
            if not self._editable:
                self._send_html(200, f"Source {sname}",
                                f"<h1>{_esc(user)} / {_esc(sname)}</h1>"
                                + history_html + _DISABLED_NOTICE)
                return
            tvals = {k: getattr(s.throttle, k) for k in s.throttle_overrides}
            self._send_html(200, f"Source {sname}", f"""
<h1>{_esc(user)} / {_esc(sname)}</h1>
{history_html}
<h2>Settings</h2>
<form method='post' action='/users/{_esc(user)}/sources' class='card'>
  <input type='hidden' name='name' value='{_esc(s.name)}'>
  <label>IMAP host <input name='host' value='{_esc(s.host)}'></label>
  <label>Port <input name='port' type='number' value='{s.port}'></label>
  <label>Username <input name='username' value='{_esc(s.username)}'></label>
  <label>New password <input name='password' type='password'
         placeholder='leave empty to keep current'></label>
  <label>&hellip;or environment variable name <input name='password_env'></label>
  <label>Gmail label <input name='label' value='{_esc(s.label)}'></label>
  <label>Folders (one per line; append <code>:: sent</code> for the sent folder (<code>auto:sent</code> finds it by its IMAP special-use attribute, any language),
    <code>:: archive</code> for label-only)
    <textarea name='folders' rows='3'>{_esc(format_folder_text(s.folders))}</textarea></label>
  <label>Backfill days on first run <input name='backfill_days' type='number' value='{s.backfill_days}'></label>
  <label>After import <select name='after_import'>
    <option value='keep' {'selected' if s.after_import == 'keep' else ''}>keep (copy — source untouched)</option>
    <option value='delete' {'selected' if s.after_import == 'delete' else ''}>delete (move — expunge after confirmed import)</option>
  </select></label>
  <div class='striplabel'>overrides (blank = inherit global settings)</div>
  <label>Poll interval (seconds, min 10)
    <input name='poll_interval_seconds' placeholder='inherit'
           value='{s.poll_interval_seconds if s.poll_interval_overridden else ""}'></label>
  <label>Bandwidth limit (KB/s)
    <input name='throttle_bandwidth_limit_kbps' placeholder='inherit'
           value='{tvals.get("bandwidth_limit_kbps", "")}'></label>
  <label>Max messages per cycle
    <input name='throttle_max_messages_per_cycle' placeholder='inherit'
           value='{tvals.get("max_messages_per_cycle", "")}'></label>
  <label>Pause between messages (s)
    <input name='throttle_message_pause_seconds' placeholder='inherit'
           value='{tvals.get("message_pause_seconds", "")}'></label>
  <button type='submit'>Save</button>
</form>
<form method='post' action='/users/{_esc(user)}/sources/{_esc(sname)}/delete'
      onsubmit="return confirm('Remove source {_esc(sname)}?')">
  <button type='submit' class='danger'>Remove this source</button>
</form>""")

    return Handler


_DISABLED_NOTICE = (
    "<div class='banner'>Configuration editing is disabled: set the "
    f"<code>{ADMIN_PASSWORD_ENV}</code> environment variable (e.g. in "
    "<code>.env</code>) and restart to enable it.</div>"
)


def start_server(bind: str, port: int, app: AppState, db: Database) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((bind, port), _make_handler(app, db))
    thread = threading.Thread(target=server.serve_forever, name="http", daemon=True)
    thread.start()
    log.info("web interface listening on %s:%d (editing %s)", bind, port,
             "enabled" if os.environ.get(ADMIN_PASSWORD_ENV) else
             f"disabled — set {ADMIN_PASSWORD_ENV}")
    return server
