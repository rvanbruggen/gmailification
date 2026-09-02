import os
import stat
import tempfile
import unittest

import yaml

from gmailification.config import ConfigError
from gmailification.config_store import ConfigStore

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
                    "password_env": "TEST_STORE_PW",
                }
            ],
        }
    ]
}


class ConfigStoreTest(unittest.TestCase):
    def setUp(self):
        os.environ["TEST_STORE_PW"] = "x"
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "config.yaml")
        self.secrets = os.path.join(self.dir.name, "secrets")
        with open(self.path, "w") as fh:
            yaml.safe_dump(BASE, fh)
        self.store = ConfigStore(self.path, self.secrets)

    def tearDown(self):
        self.dir.cleanup()

    def test_roundtrip_and_backup(self):
        raw = self.store.read_raw()
        self.store.update_globals(raw, {"poll_interval_seconds": "60"})
        cfg = self.store.write_raw(raw)
        self.assertEqual(cfg.poll_interval_seconds, 60)
        self.assertTrue(os.path.exists(self.path + ".bak"))
        self.assertEqual(self.store.read_raw()["poll_interval_seconds"], 60)

    def test_invalid_edit_rejected_and_file_untouched(self):
        raw = self.store.read_raw()
        raw["users"].append(dict(raw["users"][0]))  # duplicate user name
        with self.assertRaises(ConfigError):
            self.store.write_raw(raw)
        self.assertEqual(len(self.store.read_raw()["users"]), 1)

    def test_zero_user_config_is_valid(self):
        raw = self.store.read_raw()
        raw["users"] = []
        cfg = self.store.write_raw(raw)
        self.assertEqual(cfg.users, ())

    def test_add_and_delete_user(self):
        raw = self.store.read_raw()
        self.store.add_user(raw, "mark", "mark@example.com")
        cfg = self.store.write_raw(raw)
        self.assertEqual([u.name for u in cfg.users], ["rik", "mark"])
        self.assertEqual(cfg.user("mark").destination.token_file, "/data/tokens/mark.json")
        with self.assertRaises(ConfigError):
            self.store.add_user(raw, "mark", "other@example.com")  # duplicate
        with self.assertRaises(ConfigError):
            self.store.add_user(raw, "bad name", "a@b.c")
        self.store.delete_user(raw, "mark")
        cfg = self.store.write_raw(raw)
        self.assertEqual([u.name for u in cfg.users], ["rik"])

    def test_add_source_with_ui_password_writes_0600_file(self):
        raw = self.store.read_raw()
        self.store.upsert_source(raw, "rik", {
            "name": "trol", "host": "mail.trol.example", "username": "x@trol.example",
            "password": "test-dummy-password", "after_import": "delete",
        })
        cfg = self.store.write_raw(raw)
        src = next(s for s in cfg.user("rik").sources if s.name == "trol")
        self.assertEqual(src.password, "test-dummy-password")
        self.assertEqual(src.after_import, "delete")
        pw_path = os.path.join(self.secrets, "rik__trol.pw")
        self.assertTrue(os.path.exists(pw_path))
        self.assertEqual(stat.S_IMODE(os.stat(pw_path).st_mode), 0o600)
        # Password never lands in the YAML itself.
        with open(self.path) as fh:
            self.assertNotIn("test-dummy-password", fh.read())

    def test_upsert_source_with_sent_folder_syntax(self):
        raw = self.store.read_raw()
        self.store.upsert_source(raw, "rik", {
            "name": "telenet",
            "folders": "INBOX\n[Gmail]/Sent Mail :: sent",
        })
        cfg = self.store.write_raw(raw)
        folders = cfg.user("rik").sources[0].folders
        self.assertEqual([f.name for f in folders], ["INBOX", "[Gmail]/Sent Mail"])
        self.assertEqual([f.place for f in folders], ["inbox", "sent"])
        # Plain folders stay plain strings in the YAML; only rich ones become mappings.
        on_disk = self.store.read_raw()["users"][0]["sources"][0]["folders"]
        self.assertEqual(on_disk[0], "INBOX")
        self.assertEqual(on_disk[1], {"name": "[Gmail]/Sent Mail", "place": "sent"})

    def test_upsert_source_throttle_override_set_and_clear(self):
        raw = self.store.read_raw()
        self.store.upsert_source(raw, "rik", {
            "name": "telenet",
            "throttle_max_messages_per_cycle": "50",
            "throttle_bandwidth_limit_kbps": "",
            "throttle_message_pause_seconds": "",
        })
        cfg = self.store.write_raw(raw)
        src = cfg.user("rik").sources[0]
        self.assertEqual(src.throttle.max_messages_per_cycle, 50)
        self.assertEqual(src.throttle_overrides, ("max_messages_per_cycle",))
        # Blanking all fields reverts the source to the global throttle.
        raw = self.store.read_raw()
        self.store.upsert_source(raw, "rik", {
            "name": "telenet",
            "throttle_max_messages_per_cycle": "",
            "throttle_bandwidth_limit_kbps": "",
            "throttle_message_pause_seconds": "",
        })
        cfg = self.store.write_raw(raw)
        self.assertEqual(cfg.user("rik").sources[0].throttle_overrides, ())
        # A form without the throttle fields leaves an existing override alone.
        raw = self.store.read_raw()
        self.store.upsert_source(raw, "rik", {
            "name": "telenet", "throttle_max_messages_per_cycle": "50",
            "throttle_bandwidth_limit_kbps": "", "throttle_message_pause_seconds": "",
        })
        self.store.upsert_source(raw, "rik", {"name": "telenet", "host": "h2"})
        cfg = self.store.write_raw(raw)
        self.assertEqual(cfg.user("rik").sources[0].throttle.max_messages_per_cycle, 50)
        with self.assertRaises(ConfigError):
            self.store.upsert_source(raw, "rik", {
                "name": "telenet", "throttle_max_messages_per_cycle": "lots",
            })

    def test_upsert_source_poll_interval_set_and_clear(self):
        raw = self.store.read_raw()
        self.store.upsert_source(raw, "rik", {"name": "telenet", "poll_interval_seconds": "60"})
        cfg = self.store.write_raw(raw)
        src = cfg.user("rik").sources[0]
        self.assertEqual(src.poll_interval_seconds, 60)
        self.assertTrue(src.poll_interval_overridden)
        raw = self.store.read_raw()
        self.store.upsert_source(raw, "rik", {"name": "telenet", "poll_interval_seconds": ""})
        cfg = self.store.write_raw(raw)
        self.assertFalse(cfg.user("rik").sources[0].poll_interval_overridden)
        # Sub-minimum values are rejected by validation, file untouched.
        raw = self.store.read_raw()
        self.store.upsert_source(raw, "rik", {"name": "telenet", "poll_interval_seconds": "3"})
        with self.assertRaises(ConfigError):
            self.store.write_raw(raw)

    def test_edit_source_keeps_password_when_blank(self):
        raw = self.store.read_raw()
        self.store.upsert_source(raw, "rik", {
            "name": "telenet", "host": "imap2.example.com", "password": "", "password_env": "",
        })
        cfg = self.store.write_raw(raw)
        src = cfg.user("rik").sources[0]
        self.assertEqual(src.host, "imap2.example.com")
        self.assertEqual(src.password, "x")  # still from TEST_STORE_PW

    def test_new_source_requires_credentials(self):
        raw = self.store.read_raw()
        with self.assertRaises(ConfigError):
            self.store.upsert_source(raw, "rik", {
                "name": "nopw", "host": "h", "username": "u",
            })

    def test_delete_source(self):
        raw = self.store.read_raw()
        self.store.delete_source(raw, "rik", "telenet")
        cfg = self.store.write_raw(raw)
        self.assertEqual(cfg.user("rik").sources, ())


if __name__ == "__main__":
    unittest.main()
