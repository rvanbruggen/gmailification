"""OAuth consent flow for a destination Gmail account.

    python -m gmailification.authorize --user rik [--local | --manual]

--local  : opens a browser on this machine (admin authorizing their own account).
--manual : remote-friendly flow for family members. Prints a URL you can send
           to the user; they consent in their own browser, land on a
           http://localhost:... page that fails to load (expected!), and send
           back the full URL from their address bar. Paste it here and the
           token is saved to that user's token_file.

Tokens are written with mode 0600 to the path configured for the user
(normally on the Docker data volume) — never into the repo or image.
"""

from __future__ import annotations

import argparse
import os
import sys

from google_auth_oauthlib.flow import Flow, InstalledAppFlow

from .config import ConfigError, load_config
from .gmail_dest import SCOPES

# The manual flow pastes back a http:// localhost redirect; oauthlib refuses
# plain http unless told the transport is deliberately insecure (it is only
# used to parse the pasted URL — nothing secret travels over it).
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

MANUAL_REDIRECT = "http://localhost:8378/"


def _save_token(creds, token_file: str) -> None:
    os.makedirs(os.path.dirname(token_file) or ".", exist_ok=True)
    fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(creds.to_json())
    print(f"Token saved to {token_file}")


def run_local(client_file: str, token_file: str) -> None:
    flow = InstalledAppFlow.from_client_secrets_file(client_file, SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    _save_token(creds, token_file)


def run_manual(client_file: str, token_file: str, user: str, email: str) -> None:
    flow = Flow.from_client_secrets_file(client_file, SCOPES, redirect_uri=MANUAL_REDIRECT)
    auth_url, _state = flow.authorization_url(
        access_type="offline", prompt="consent", login_hint=email)
    print()
    print(f"=== Authorization needed for {user} ({email}) ===")
    print()
    print("1. Send this URL to the account owner (or open it yourself):")
    print()
    print(f"   {auth_url}")
    print()
    print(f"2. They must sign in as {email} and approve. The browser will then try")
    print(f"   to open {MANUAL_REDIRECT} and show a 'can't connect' error — that is")
    print("   expected and fine.")
    print("3. Ask them to copy the FULL URL from the browser's address bar")
    print("   (it starts with http://localhost:8378/?state=...) and send it back.")
    print()
    pasted = input("Paste that full URL here: ").strip()
    if not pasted:
        print("No URL pasted, aborting.", file=sys.stderr)
        sys.exit(1)
    flow.fetch_token(authorization_response=pasted)
    _save_token(flow.credentials, token_file)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gmailification.authorize")
    parser.add_argument("--config", default=os.environ.get("GMAILIFICATION_CONFIG", "/config/config.yaml"))
    parser.add_argument("--user", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--local", action="store_true", help="open a browser on this machine")
    mode.add_argument("--manual", action="store_true", help="print a URL / paste the redirect back (default)")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
        user = cfg.user(args.user)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    if not os.path.exists(cfg.oauth_client_file):
        print(f"OAuth client file not found: {cfg.oauth_client_file}\n"
              "Download the desktop-app OAuth client JSON from the Google Cloud console\n"
              "and mount it at that path.", file=sys.stderr)
        return 2

    if args.local:
        run_local(cfg.oauth_client_file, user.destination.token_file)
    else:
        run_manual(cfg.oauth_client_file, user.destination.token_file,
                   user.name, user.destination.email)
    return 0


if __name__ == "__main__":
    sys.exit(main())
