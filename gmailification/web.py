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
from .config import AppConfig, ConfigError, load_config
from .config_store import ConfigStore
from .gmail_dest import GmailDestination, token_info
from .state import Database

log = logging.getLogger("gmailification.web")

ADMIN_PASSWORD_ENV = "GMAILIFICATION_ADMIN_PASSWORD"


class Shared:
    """State shared between the main loop and the HTTP server."""

    def __init__(self, poll_interval: int):
        self.poll_interval = poll_interval
        self.last_cycle_at: float | None = None
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

    def healthy(self) -> bool:
        if self.last_cycle_at is None:
            return False
        return time.time() - self.last_cycle_at < max(3 * self.poll_interval, 600)


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
:root { color-scheme: light dark; }
body { font-family: -apple-system, system-ui, sans-serif; margin: 2rem auto;
       max-width: 60rem; padding: 0 1rem; line-height: 1.5; }
h1 { font-size: 1.4rem; } h2 { font-size: 1.15rem; margin-top: 2rem; }
a { color: #2563eb; } .muted { opacity: .65; font-size: .85rem; }
.card { border: 1px solid rgba(128,128,128,.35); border-radius: 8px;
        padding: 1rem 1.25rem; margin: .75rem 0; }
.ok { color: #16a34a; } .bad { color: #dc2626; } .warn { color: #d97706; }
table { border-collapse: collapse; width: 100%; }
td, th { text-align: left; padding: .3rem .6rem .3rem 0; vertical-align: top;
         border-bottom: 1px solid rgba(128,128,128,.2); }
form.inline { display: inline; }
label { display: block; margin-top: .6rem; font-size: .9rem; }
input, select { padding: .3rem .4rem; margin-top: .15rem; min-width: 16rem; }
button { padding: .35rem .9rem; margin-top: .8rem; cursor: pointer; }
button.danger { color: #dc2626; }
.banner { background: rgba(217,119,6,.12); border: 1px solid #d97706;
          border-radius: 8px; padding: .6rem 1rem; margin: 1rem 0; }
code { background: rgba(128,128,128,.15); padding: .1rem .3rem; border-radius: 4px; }
"""


def _page(title: str, body: str) -> bytes:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{_esc(title)}</title><style>{_STYLE}</style></head><body>"
        f"<p class='muted'><a href='/'>gmailification</a> v{__version__} &middot; "
        f"<a href='/config'>settings</a></p>"
        f"{body}</body></html>"
    ).encode("utf-8")


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
            if path == "/healthz":
                ok = app.shared.healthy()
                self._send_json(200 if ok else 503, {
                    "status": "ok" if ok else "stale",
                    "version": __version__,
                    "last_cycle_at": app.shared.last_cycle_at,
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
            body = ["<h1>Dashboard</h1>"]
            if not self._editable:
                body.append(_DISABLED_NOTICE)
            last = app.shared.last_cycle_at
            body.append(f"<p class='muted'>Last cycle: "
                        f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last)) if last else 'not yet'}"
                        f" &middot; polling every {cfg.poll_interval_seconds}s</p>")
            if self._editable:
                body.append("<form class='inline' method='post' action='/poll'>"
                            "<button type='submit'>Poll all now</button></form>")
            for u in cfg.users:
                state, detail = token_info(u.destination.token_file)
                tok_cls = "ok" if state in ("ok", "refreshable") else "bad"
                rows = []
                for s in u.sources:
                    st = statuses.get(s.key)
                    if st is None or st.last_success_at is None and st.failing_since is None:
                        health = "<span class='muted'>no data yet</span>"
                    elif st.failing_since is None:
                        health = "<span class='ok'>&#10003; healthy</span>"
                    else:
                        health = f"<span class='bad'>&#10007; failing: {_esc(st.last_error or '')}</span>"
                    mode = "move" if s.after_import == "delete" else "copy"
                    rows.append(f"<tr><td>{_esc(s.name)}</td><td>{_esc(s.username)}</td>"
                                f"<td>{mode}</td><td>{health}</td></tr>")
                poll_btn = (f"<form class='inline' method='post' action='/poll?user={_esc(u.name)}'>"
                            f"<button type='submit'>Poll now</button></form>"
                            if self._editable else "")
                body.append(f"""
<div class='card'>
<h2><a href='/users/{_esc(u.name)}'>{_esc(u.name)}</a>
    <span class='muted'>&rarr; {_esc(u.destination.email)}</span></h2>
<p>Gmail token: <span class='{tok_cls}'>{_esc(state)}</span>
   <span class='muted'>({_esc(detail)})</span></p>
<table><tr><th>source</th><th>account</th><th>mode</th><th>health</th></tr>
{''.join(rows) or '<tr><td colspan=4 class="muted">no sources</td></tr>'}</table>
{poll_btn}
</div>""")
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
  <label>Poll interval (seconds)
    <input name='poll_interval_seconds' type='number' min='30' value='{cfg.poll_interval_seconds}'></label>
  <label>Alert after (hours failing)
    <input name='alert_after_hours' type='number' step='0.5' value='{cfg.alert_after_hours}'></label>
  <label>Re-alert every (hours)
    <input name='realert_after_hours' type='number' step='0.5' value='{cfg.realert_after_hours}'></label>
  <h2>Throttling</h2>
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
            tok_cls = "ok" if state in ("ok", "refreshable") else "bad"
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
  <label>Folders (comma-separated) <input name='folders' value='INBOX'></label>
  <label>Backfill days on first run <input name='backfill_days' type='number' value='0'></label>
  <label>After import <select name='after_import'>
    <option value='keep'>keep (copy — source untouched)</option>
    <option value='delete'>delete (move — expunge after confirmed import)</option>
  </select></label>
  <button type='submit'>Add source</button>
</form>
<form method='post' action='/users/{_esc(name)}/delete'
      onsubmit="return confirm('Remove user {_esc(name)} and all their sources from the config?')">
  <button type='submit' class='danger'>Remove this user</button>
</form>""" if editable else _DISABLED_NOTICE
            self._send_html(200, f"User {name}", f"""
<h1>{_esc(name)} <span class='muted'>&rarr; {_esc(u.destination.email)}</span></h1>
<p>Gmail token: <span class='{tok_cls}'>{_esc(state)}</span>
   <span class='muted'>({_esc(detail)})</span> {oauth_btn}</p>
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
            if not self._editable:
                self._send_html(200, f"Source {sname}",
                                f"<h1>{_esc(user)} / {_esc(sname)}</h1>" + _DISABLED_NOTICE)
                return
            self._send_html(200, f"Source {sname}", f"""
<h1>{_esc(user)} / {_esc(sname)}</h1>
<form method='post' action='/users/{_esc(user)}/sources' class='card'>
  <input type='hidden' name='name' value='{_esc(s.name)}'>
  <label>IMAP host <input name='host' value='{_esc(s.host)}'></label>
  <label>Port <input name='port' type='number' value='{s.port}'></label>
  <label>Username <input name='username' value='{_esc(s.username)}'></label>
  <label>New password <input name='password' type='password'
         placeholder='leave empty to keep current'></label>
  <label>&hellip;or environment variable name <input name='password_env'></label>
  <label>Gmail label <input name='label' value='{_esc(s.label)}'></label>
  <label>Folders (comma-separated) <input name='folders' value='{_esc(", ".join(s.folders))}'></label>
  <label>Backfill days on first run <input name='backfill_days' type='number' value='{s.backfill_days}'></label>
  <label>After import <select name='after_import'>
    <option value='keep' {'selected' if s.after_import == 'keep' else ''}>keep (copy — source untouched)</option>
    <option value='delete' {'selected' if s.after_import == 'delete' else ''}>delete (move — expunge after confirmed import)</option>
  </select></label>
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
