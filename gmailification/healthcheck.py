"""Docker HEALTHCHECK entrypoint: exit 0 iff /healthz says ok."""

import os
import sys
import urllib.request


def main() -> int:
    port = int(os.environ.get("GMAILIFICATION_HTTP_PORT", "8377"))
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as resp:
            return 0 if resp.status == 200 else 1
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
