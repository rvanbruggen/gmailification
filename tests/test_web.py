import base64
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request

import yaml

from gmailification.state import Database
from gmailification.web import ADMIN_PASSWORD_ENV, AppState, start_server

BASE = {
    "users": [
        {
            "name": "rik",
            "destination": {"email": "rik@example.com", "token_file": "/data/tokens/rik.json"},
            "sources": [
                {
                    "name": "telenet",
                    "host": "imap.example.com",
                    "username": "rik@example.net",
                    "password_env": "TEST_WEB_PW",
                }
            ],
        }
    ]
}


class WebTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["TEST_WEB_PW"] = "x"
        os.environ[ADMIN_PASSWORD_ENV] = "test-admin-password"
        cls.dir = tempfile.TemporaryDirectory()
        cls.config_path = os.path.join(cls.dir.name, "config.yaml")
        raw = dict(BASE)
        raw["secrets_dir"] = os.path.join(cls.dir.name, "secrets")
        raw["db_path"] = os.path.join(cls.dir.name, "state.db")
        with open(cls.config_path, "w") as fh:
            yaml.safe_dump(raw, fh)
        cls.app = AppState(cls.config_path)
        cls.db = Database(cls.app.cfg.db_path)
        cls.server = start_server("127.0.0.1", 0, cls.app, cls.db)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.dir.cleanup()
        os.environ.pop(ADMIN_PASSWORD_ENV, None)

    def _request(self, path, data=None, auth=True, headers=None):
        req = urllib.request.Request(self.base + path, data=data, headers=headers or {})
        if auth:
            cred = base64.b64encode(b"admin:test-admin-password").decode()
            req.add_header("Authorization", f"Basic {cred}")
        return urllib.request.urlopen(req, timeout=10)

    def test_healthz_open_and_versioned(self):
        try:
            resp = self._request("/healthz", auth=False)
            payload = json.load(resp)
        except urllib.error.HTTPError as exc:  # 503 before first cycle
            payload = json.load(exc)
            self.assertEqual(exc.code, 503)
        self.assertIn("version", payload)

    def test_status_open(self):
        payload = json.load(self._request("/status", auth=False))
        self.assertIn("sources", payload)
        self.assertIn("version", payload)

    def test_dashboard_requires_auth(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._request("/", auth=False)
        self.assertEqual(ctx.exception.code, 401)
        html = self._request("/").read().decode()
        self.assertIn("rik@example.com", html)
        self.assertIn("telenet", html)

    def test_poll_endpoint_sets_force_event(self):
        self.app.shared.force_event.clear()
        resp = self._request("/poll?user=rik", data=b"", auth=False)
        self.assertEqual(resp.status, 202)
        self.assertTrue(self.app.shared.force_event.is_set())
        self.assertEqual(self.app.shared.take_poll_request(), "rik")

    def test_add_edit_delete_source_via_forms(self):
        body = ("name=trol&host=mail.trol.example&port=993&username=x%40trol.example"
                "&password=test-dummy-password&label=&folders=INBOX&backfill_days=0&after_import=keep")
        resp = self._request("/users/rik/sources", data=body.encode())
        self.assertEqual(resp.status, 200)  # after redirect to the user page
        cfg, _ = self.app.snapshot()
        names = [s.name for s in cfg.user("rik").sources]
        self.assertIn("trol", names)
        # The config file on disk was rewritten and validated.
        with open(self.config_path) as fh:
            on_disk = fh.read()
        self.assertIn("trol", on_disk)
        self.assertNotIn("test-dummy-password", on_disk)
        # Edit: switch to move semantics.
        body = "name=trol&after_import=delete"
        self._request("/users/rik/sources", data=body.encode())
        cfg, _ = self.app.snapshot()
        src = next(s for s in cfg.user("rik").sources if s.name == "trol")
        self.assertEqual(src.after_import, "delete")
        # Delete.
        self._request("/users/rik/sources/trol/delete", data=b"")
        cfg, _ = self.app.snapshot()
        self.assertNotIn("trol", [s.name for s in cfg.user("rik").sources])

    def test_favicon_served_without_auth(self):
        resp = self._request("/favicon.svg", auth=False)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers["Content-Type"], "image/svg+xml")
        self.assertIn(b"<svg", resp.read())

    def test_history_endpoint(self):
        self.db.record_poll("rik/histtest", "rik", ok=True, imported=2)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._request("/history", auth=False)
        self.assertEqual(ctx.exception.code, 401)
        payload = json.load(self._request("/history?hours=1&source=rik/histtest"))
        self.assertEqual(len(payload["polls"]), 1)
        self.assertEqual(payload["polls"][0]["imported"], 2)

    def test_dashboard_renders_strip_and_activity(self):
        self.db.record_poll("rik/telenet", "rik", ok=False, error="kaput")
        html = self._request("/").read().decode()
        self.assertIn("svg class='strip'", html.replace('"', "'"))
        self.assertIn("kaput", html)

    def test_invalid_edit_returns_400(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._request("/users/rik/sources", data=b"name=nopw&host=h&username=u")
        self.assertEqual(ctx.exception.code, 400)

    def test_global_settings_update_hot_reloads(self):
        old = self.app.cfg.poll_interval_seconds
        self._request("/config", data=b"poll_interval_seconds=61")
        cfg, _ = self.app.snapshot()
        self.assertEqual(cfg.poll_interval_seconds, 61)
        self.assertEqual(self.app.shared.poll_interval, 61)
        self._request("/config", data=f"poll_interval_seconds={old}".encode())

    def test_cross_origin_post_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._request("/config", data=b"poll_interval_seconds=55",
                          headers={"Origin": "http://evil.example"})
        self.assertEqual(ctx.exception.code, 403)
        cfg, _ = self.app.snapshot()
        self.assertNotEqual(cfg.poll_interval_seconds, 55)


if __name__ == "__main__":
    unittest.main()
