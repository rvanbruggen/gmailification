# Changelog

All notable changes to gmailification are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow
[Semantic Versioning](https://semver.org/).

The current version lives in `gmailification/__init__.py` (`__version__`) and
is reported by the service at startup (log line), in the `/healthz` and
`/status` endpoints, and in the footer of alert emails.

## [Unreleased]

## [0.6.0] - 2026-09-02

### Added
- Per-source poll intervals: a source may set `poll_interval_seconds`
  (minimum 10) to override the global default; the scheduler now tracks a
  per-source due time and wakes for whatever is due, so a time-critical
  mailbox can poll every minute while a quiet one polls hourly. Editable per
  source in the web UI (blank = inherit the global interval).

### Changed
- The main loop sleeps until the earliest due source (capped at the global
  interval so heartbeat/healthcheck stay fresh); forced polls (`POST /poll`)
  still run immediately regardless of schedules.

## [0.5.0] - 2026-09-02

### Added
- Per-source throttle overrides: a source's `throttle:` block overrides any
  subset of the global `bandwidth_limit_kbps` / `max_messages_per_cycle` /
  `message_pause_seconds`; unset fields inherit the global values. Editable
  in the web UI on each source page (blank = inherit). Designed for large
  one-off archive imports that should crawl while live sources stay fast.

### Changed
- Unknown fields in a `throttle:` block are now rejected at config load.

## [0.4.1] - 2026-09-02

### Added
- The web UI now serves the gmailification logo as its favicon
  (`/favicon.svg`, SVG, unauthenticated, cached for a day).

## [0.4.0] - 2026-09-02

### Added
- Poll history: every cycle is recorded per source (outcome, messages
  imported/deduped/moved, duration, error) in SQLite, pruned after
  `history_days` (default 14).
- History visualization in the web UI: 24-hour strips per source on the
  dashboard, 24-hour + N-day strips and a noteworthy-events table on each
  source page, and a "Recent activity" feed on the dashboard. Tick state is
  double-encoded (color + height) with text tooltips for accessibility.
- `GET /history?hours=&source=` JSON endpoint (admin-authenticated).

### Changed
- Full web UI redesign: logo, header navigation, design tokens with dark-mode
  support, cards, status pills, and restyled forms/tables — still pure
  server-rendered HTML/CSS, no frameworks, no build step.

## [0.3.0] - 2026-09-02

### Added
- Per-folder placement: each source folder can be a plain name (unchanged
  behavior) or a mapping with `place: inbox | sent | archive` and an optional
  per-folder `label` override. `sent` imports into Gmail's Sent view (read,
  threaded, never in the inbox) via the SENT system label — sync a source's
  sent folder and conversations in the destination show both sides. `archive`
  imports label-only (All Mail, read).
- Web UI folder field accepts the compact syntax, one folder per line:
  `name [:: place [:: label]]`; plain comma-separated names keep working.

### Changed
- Duplicate folder names within one source are now rejected at config load.

## [0.2.2] - 2026-09-02

### Fixed
- Docker: `/data` is now created and chowned to the service user in the
  image, so the named volume is writable on first use (previously the
  container crash-looped with `sqlite3.OperationalError: unable to open
  database file`).
- Startup now prints an actionable error instead of a traceback when the
  state database cannot be opened.

### Added
- Troubleshooting section in the README (volume ownership, literal `$` in
  `.env` values, app-password formatting).

## [0.2.1] - 2026-09-01

### Changed
- Documentation and example config now use generic placeholder accounts
  (alice/bob @ *.example) instead of the original author's real mailboxes;
  the git history was rewritten to scrub those addresses as well.

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
