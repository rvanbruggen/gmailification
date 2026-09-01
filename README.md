# gmailification

A small, self-hosted service that **pulls** mail from secondary mailboxes into
Gmail — a replacement for Gmail's discontinued "Check mail from other
accounts" / Gmailify feature (removed January 2026), and for the unreliable
forwarding rules that break on SPF/DMARC checks.

Instead of having mail *pushed* (forwarded) to Gmail, gmailification periodically
polls each secondary mailbox over IMAP and delivers new messages into the
destination Gmail account via the Gmail API's `users.messages.import` — so
mail arrives exactly as if delivered normally: it threads, it's searchable,
spam classification applies, and nothing travels over SMTP, so there are no
forwarding penalties and no lost mail.

**Multi-tenant:** one instance serves several people (e.g. a family on a LAN),
each with their own destination Gmail account and their own set of source
mailboxes. Adding a user or a source is a config change, not a code change.

## Design guarantees

- **Copy or move — your choice, per source.** The default (`after_import: keep`)
  never modifies the source: folders are opened read-only and messages fetched
  with `BODY.PEEK[]` — no flags set, nothing moved or deleted; stop the service
  at any time and both sides are fully intact. A source can instead opt into
  move semantics (`after_import: delete`): a message is expunged from the
  source *only after* its Gmail import succeeded and was recorded — anything
  that failed to transfer always stays put.
- **Narrowest possible Gmail access.** The OAuth scopes are
  `gmail.insert` + `gmail.labels`: gmailification can *add* mail and manage labels,
  and can never read, modify, or delete existing mail in anyone's Gmail.
- **Idempotent.** Per-folder UID cursors plus a per-user Message-ID dedupe
  table (SQLite) mean restarts, re-runs, and even server-side mailbox rebuilds
  (UIDVALIDITY changes) never produce duplicates.
- **Strict tenant isolation.** A source is owned by exactly one user and can
  only ever deliver into that user's destination; state and alerts are scoped
  per user.
- **No silent failure.** A source that keeps failing triggers an alert email
  into its owner's own inbox (delivered through the same import path — no send
  permission needed), with a copy to the admin. OAuth breakage alerts the
  admin. Plus a Docker healthcheck and `/status` endpoint.
- **Polite on shared broadband.** Optional throttling: bandwidth cap,
  per-cycle message cap, inter-message pause (see `throttle:` in the config).

### Trust note (read this if you share an instance)

The person who runs the host (the admin) technically holds every user's
source passwords and Gmail OAuth tokens on that box. Within a family that is
usually fine — but it should be said out loud. Mitigations: secrets live only
in `.env` / mounted files and on the Docker volume (never in the image, repo,
or logs), token files are written mode `0600`, and the OAuth scopes mean a
stolen token could add mail to an inbox but never read it.

## How it works

```
   ┌────────────────────────  poll (IMAP, read-only)  ───────────────────┐
   │                                                                     │
   │   alice@old-company.example     (Google Workspace, app password) ──┐        │
   │   alice@legacy.example    (Google Workspace, app password) ──┤        │
   │   alice@isp.example  (plain IMAP) ─────────────────┼──► alice@gmail.com
   │   club@association.example       (plain IMAP) ─────────────────┘   (Gmail API messages.import,
   │                                                                 labels Pulled/<source>)
   │   bob@old-company.example    (Google Workspace, app password) ──┐
   │   info@association.example         (plain IMAP) ─────────────────┼──► bob@gmail.com
   └────────────────────────────────────────────────────────────┘
```

One code path for all sources: IMAP polling (default every 3 minutes). Google
account sources authenticate with app passwords (still supported in 2026;
requires 2-Step Verification), so the legacy Workspace accounts themselves are
never reconfigured. Each destination account grants its own OAuth consent;
one shared Google Cloud OAuth client, tokens per user.

Each poll cycle runs every source in its own worker thread with hard socket
timeouts — one dead source delays nothing else. Transient errors retry with
exponential backoff; per-source health counters feed the alerting.

**First run:** by default gmailification starts from "now" — it does not import
years of existing mail. Set `backfill_days: N` on a source to import a window
of history on its first run.

## Installation

### 0. Prerequisites

- A machine that is always on (home server, NAS, Raspberry Pi, …) with
  **Docker + docker compose** installed. Everything runs in one container.
- A destination **Gmail account** per user, and the credentials of the
  source mailboxes you want to pull from.
- Port 8377 free on the host (the web interface — keep it LAN-only).

```bash
git clone https://github.com/rvanbruggen/gmailification.git
cd gmailification
```

### 1. Google Cloud project (one-time, admin)

1. Create a project at console.cloud.google.com, enable the **Gmail API**.
2. Configure the OAuth consent screen (External). Add each destination Gmail
   address as a test user — or publish the app (unverified is fine for
   personal use) so refresh tokens don't expire every 7 days in testing mode.
3. Create an **OAuth client ID** of type **Desktop app** and download the JSON
   as `config/client_secret.json`.

### 2. App passwords for Google-account sources (each account owner)

On each source Google account: enable 2-Step Verification, then create an app
password at `myaccount.google.com/apppasswords`. That 16-character password is
what goes in `.env`. Nothing else about the account is touched.

### 3. Configure

The easy path — start empty and do everything in the web UI later:

```bash
echo "users: []" > config/config.yaml
cp .env.example .env    # set at least GMAILIFICATION_ADMIN_PASSWORD
```

Or, if you prefer files, start from the annotated example and edit it:

```bash
cp config/config.example.yaml config/config.yaml   # edit users/sources
cp .env.example .env                               # fill in source passwords
```

Either way: `config/config.yaml`, `.env`, `config/client_secret.json`, and
everything under `data/` are gitignored — no credentials can end up in the
repo.

### 4. Authorize each destination Gmail

Easiest: start the service (step 5), open the web UI, and click
**Authorize Gmail** on the user's page — it walks you through the consent
flow, including sending the consent URL to a family member who isn't at the
machine. The equivalent CLI also exists:

```bash
docker compose run --rm gmailification python -m gmailification.authorize --user rik --local
```

(`--manual` instead of `--local` for the remote flow: it prints a URL you can
send them; they consent in their own browser, land on a `localhost` page that
fails to load (expected), and send you back the full URL from their address
bar; paste it into the prompt and their token is saved.) The same flows handle
re-authorization — and if a token ever expires or is revoked, gmailification
alerts the admin.

### 5. Run

```bash
docker compose up -d --build
```

```bash
docker compose logs -f gmailification
```

That's the whole installation: one container, state on a Docker volume,
config and secrets mounted from the host, nothing else to deploy.

## How to use

**First run (10 minutes):**

1. Open `http://<docker-host>:8377/` and log in with the admin password you
   set in `.env` (any username).
2. For each person, the dashboard shows a card. Add missing users with
   **Add user** (name + destination Gmail address).
3. On a user's page, click **Authorize Gmail…** and follow the three steps
   shown — for a family member elsewhere, just send them the consent link and
   paste back the URL they land on. The token status flips to `ok`.
4. Add their sources: hostname, username, password (for Google-account
   sources: an app password), and choose **keep** (copy — source stays
   untouched) or **delete** (move — drained into Gmail after each confirmed
   import). Optionally set backfill days to import recent history.
5. Click **Poll now** — within seconds new mail appears in the destination
   Gmail under its `Pulled/<source>` label.

**Day to day** there is nothing to do — that's the point:

- Mail from every source simply arrives in Gmail, labeled `Pulled/<source>`,
  threaded and searchable, on the default 3-minute cycle.
- Expecting a 2FA code or an urgent mail? Hit **Poll now** on the dashboard
  (or `POST /poll`).
- If a source breaks (password changed, server down), its owner gets an
  alert email in their own inbox after a few hours — with instructions —
  and again every day until it recovers. No news is good news.
- If a Gmail token ever needs re-consent, the admin is alerted; fixing it is
  the same **Authorize Gmail…** button.
- Config changes (new source, new user, throttling) are made in the web UI
  and take effect immediately — or edit `config/config.yaml` by hand and
  restart the container, both work.

## Web interface

`http://dockerhost:8377/` serves a small built-in web UI (no framework, no
build step, served by the service itself):

- **Dashboard** — per-user cards with Gmail token state and per-source health,
  plus "Poll now" buttons (the *my-2FA-code-is-arriving-right-now* button).
- **Configuration** — add/remove users and sources, edit source settings
  (host, credentials, label, folders, backfill, copy-vs-move), global
  settings and throttling. Every edit is validated with the full config
  parser before being written (atomically, keeping a `.bak`), and the running
  service hot-reloads — no restart needed.
- **Gmail authorization** — start the OAuth consent flow from the browser: it
  shows the URL to send to the account owner and a field to paste their
  redirect URL back into; the token is saved and picked up immediately.

Editing requires `GMAILIFICATION_ADMIN_PASSWORD` to be set in `.env` (HTTP
Basic Auth, any username). While it is unset the UI is read-only and all
configuration routes are disabled. Passwords entered in the UI are stored as
mode-0600 files under `/data/secrets/` and referenced from the YAML — they
never appear in the config file, the repo, or the logs. One caveat: a UI save
rewrites `config.yaml`, so hand-written YAML comments don't survive it.

## Operations

| What | How |
|---|---|
| Web UI | `http://dockerhost:8377/` |
| Health (Docker) | built-in `HEALTHCHECK` hits `/healthz` |
| Health (machine) | `curl http://dockerhost:8377/status` — per-source counters and errors |
| Force a poll *now* (e.g. a 2FA code) | UI button, or `curl -X POST http://dockerhost:8377/poll` (optionally `?user=rik`) |
| One-shot run | `docker compose run --rm gmailification --once` |
| Logs as JSON | set `GMAILIFICATION_LOG_JSON=1` in `.env` |

`/healthz`, `/status` and `POST /poll` are unauthenticated (harmless,
read-only or poll-triggering); everything else sits behind the admin
password. Keep port 8377 LAN-only regardless — firewall it or bind it to
`127.0.0.1` in `docker-compose.yml` if your Docker host is reachable from
outside.

Alerts arrive in the affected user's own inbox under the `Pulled/alerts`
label after a source has been failing for `alert_after_hours` (default 6),
repeating every `realert_after_hours` (default 24) until recovery.

## Design decisions

- **Build, not fork.** [turbogmailify](https://github.com/YoRyan/turbogmailify)
  (MIT, Go) is the closest prior art and validated the `messages.import`
  approach, but it is single-destination (no multi-tenancy) and *always
  destructive* — it unconditionally deletes/archives source messages after
  import, whereas here that is a per-source opt-in with untouched-source as
  the default. [pop2gmail](https://github.com/beZong/pop2gmail)
  turns out to use IMAP APPEND with app passwords rather than the Gmail API.
  Neither was a viable base; both informed the design.
- **`messages.import`, not `insert`, not SMTP.** Import gives standard
  delivery scanning/classification and threading with no SMTP hop. Caveat:
  Gmail *filters* are applied by Gmail's delivery pipeline and may not run on
  API-imported mail exactly as on SMTP-delivered mail — the per-source
  `Pulled/*` labels are the reliable routing signal.
- **stdlib `imaplib` instead of an IMAP library.** We need exact raw bytes
  (`BODY.PEEK[]`) and strictly read-only semantics; doing it directly keeps
  dependencies at five mainstream packages and the wire behavior auditable.
- **App passwords for Workspace sources.** Verified still supported (Google's
  2025 basic-auth turndown explicitly exempts app passwords). If Google ever
  removes them, the fallback is per-account XOAUTH2 for IMAP.

## v2 (designed for, not built): outbound sending

Gmail's "Send mail as" for external addresses ends January 2027. The planned
answer is a companion **reply gateway**: outbound mail for each secondary
address submitted via that provider's own SMTP (correct SPF/DKIM by
construction). The multi-tenant config model (per-user sources with
credentials) is deliberately shaped so an `smtp:` block per source can slot
in; the open UX question is how to trigger it from the Gmail UI (drafts-folder
convention, or a small browser extension talking to this LAN service).

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests
```

### Versioning & releases

The single source of truth for the version is `__version__` in
[gmailification/__init__.py](gmailification/__init__.py). It is surfaced at
startup in the logs, by `/healthz` and `/status`, and in alert email footers.
Every release must:

1. Bump `__version__` and add a dated entry to [CHANGELOG.md](CHANGELOG.md).
2. Update this README if behavior or configuration changed.
3. Tag and push: `git tag v<version> && git push origin main --follow-tags`.

## License

[MIT](LICENSE)
