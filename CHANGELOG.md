# Changelog

All notable changes to gmailification are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow
[Semantic Versioning](https://semver.org/).

The current version lives in `gmailification/__init__.py` (`__version__`) and
is reported by the service at startup (log line), in the `/healthz` and
`/status` endpoints, and in the footer of alert emails.

## [Unreleased]

## [0.2.0] - 2026-09-01

### Added
- Built-in web interface at `http://<host>:8377/`: dashboard (per-user cards,
  token state, per-source health, "Poll now" buttons) and a full
  configuration editor — add/remove users and sources, edit source settings
  including copy-vs-move (`after_import`), global settings and throttling.
- Configuration edits are validated with the full config parser before being
  written (atomic write with `.bak`), and the running service hot-reloads
  without a restart.
- Gmail OAuth consent flow in the browser (start flow, send the URL to the
  account owner, paste the redirect back) — the CLI flow still exists.
- Web UI authentication: HTTP Basic Auth via `GMAILIFICATION_ADMIN_PASSWORD`;
  while unset, the UI is read-only and editing is disabled. Cross-origin
  POSTs are rejected. `/healthz`, `/status` and `POST /poll` stay open.
- Passwords entered in the UI are stored as mode-0600 files under
  `/data/secrets/`, never in the YAML.

### Changed
- A zero-user config (`users: []`) is now valid, so a fresh install can start
  up and be configured entirely through the web UI.
- The `/config` bind mount in docker-compose is now read-write so UI edits
  can be saved (host directory must be writable by uid 1000).
- Internal: the health endpoint module was folded into the new `web.py`.

## [0.1.0] - 2026-09-01

### Added
- Initial release: multi-tenant IMAP → Gmail consolidator.
- One IMAP polling code path for all sources (Google Workspace via app
  passwords, plain IMAP), read-only by default (`BODY.PEEK[]`).
- Delivery via Gmail API `users.messages.import` with per-source `Pulled/*`
  labels (auto-created); OAuth scopes limited to `gmail.insert` + `gmail.labels`.
- Per-source `after_import: keep | delete` — copy semantics (default, source
  untouched) or move semantics (expunge from source only after a confirmed,
  recorded import).
- SQLite state: per-folder UID cursors with UIDVALIDITY handling, per-user
  Message-ID dedupe, per-source health counters.
- Multi-tenant YAML config (users → destination + sources); per-user OAuth
  tokens; remote-friendly consent flow (`python -m gmailification.authorize
  --user X --manual`).
- Alerting into the affected user's own inbox via the import path (admin gets
  copies and OAuth-breakage alerts); configurable thresholds.
- LAN HTTP endpoint: `/healthz`, `/status` (both report the version),
  `POST /poll` for on-demand polls; Docker healthcheck.
- Throttling: bandwidth cap, per-cycle message cap, inter-message pause.
- Docker image + docker-compose deployment; secrets only via `.env` /
  mounted files / Docker volume, all gitignored.
