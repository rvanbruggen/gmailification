import os
import tempfile
import unittest

from gmailification.config import (
    ConfigError,
    FolderConfig,
    format_folder_text,
    load_config,
    parse_folder_text,
)

MINIMAL = """
users:
  - name: rik
    destination:
      email: rik@example.com
      token_file: /data/tokens/rik.json
    sources:
      - name: telenet
        host: imap.example.com
        username: rik@example.net
        password_env: TEST_GMAILIFICATION_PW
"""


class ConfigTest(unittest.TestCase):
    def _load(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write(text)
            path = fh.name
        try:
            return load_config(path)
        finally:
            os.unlink(path)

    def test_minimal_config(self):
        os.environ["TEST_GMAILIFICATION_PW"] = "test-env-password"
        cfg = self._load(MINIMAL)
        self.assertEqual(len(cfg.users), 1)
        src = cfg.users[0].sources[0]
        self.assertEqual(src.key, "rik/telenet")
        self.assertEqual(src.password, "test-env-password")
        self.assertEqual(src.label, "Pulled/telenet")  # defaulted from name
        self.assertEqual(src.folders, (FolderConfig(name="INBOX"),))
        self.assertEqual(cfg.poll_interval_seconds, 180)
        self.assertEqual(cfg.throttle.max_messages_per_cycle, 200)

    def test_missing_env_var(self):
        os.environ.pop("TEST_GMAILIFICATION_PW", None)
        with self.assertRaises(ConfigError) as ctx:
            self._load(MINIMAL)
        self.assertIn("TEST_GMAILIFICATION_PW", str(ctx.exception))

    def test_password_file(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as fh:
            fh.write("test-file-password\n")
            pw_path = fh.name
        try:
            cfg = self._load(MINIMAL.replace(
                "password_env: TEST_GMAILIFICATION_PW", f"password_file: {pw_path}"))
            self.assertEqual(cfg.users[0].sources[0].password, "test-file-password")
        finally:
            os.unlink(pw_path)

    def test_duplicate_source_names_rejected(self):
        os.environ["TEST_GMAILIFICATION_PW"] = "x"
        dup = MINIMAL + """
      - name: telenet
        host: other.example.com
        username: other@example.net
        password_env: TEST_GMAILIFICATION_PW
"""
        with self.assertRaises(ConfigError):
            self._load(dup)

    def test_unknown_admin_rejected(self):
        os.environ["TEST_GMAILIFICATION_PW"] = "x"
        with self.assertRaises(ConfigError):
            self._load(MINIMAL + "\nadmin:\n  user: nobody\n")

    def test_folder_mappings(self):
        os.environ["TEST_GMAILIFICATION_PW"] = "x"
        cfg = self._load(MINIMAL + """
        folders:
          - INBOX
          - name: "[Gmail]/Sent Mail"
            place: sent
          - name: Old
            place: archive
            label: Pulled/old
""")
        folders = cfg.users[0].sources[0].folders
        self.assertEqual(folders[0], FolderConfig(name="INBOX"))
        self.assertEqual(folders[1], FolderConfig(name="[Gmail]/Sent Mail", place="sent"))
        self.assertEqual(folders[2], FolderConfig(name="Old", place="archive", label="Pulled/old"))

    def test_auto_folder_names(self):
        os.environ["TEST_GMAILIFICATION_PW"] = "x"
        cfg = self._load(MINIMAL + """
        folders:
          - INBOX
          - name: "AUTO:Sent"
            place: sent
""")
        folders = cfg.users[0].sources[0].folders
        self.assertEqual(folders[1].name, "auto:sent")  # normalized
        with self.assertRaises(ConfigError):
            self._load(MINIMAL + "\n        folders: [\"auto:outbox\"]\n")

    def test_invalid_folder_place_rejected(self):
        os.environ["TEST_GMAILIFICATION_PW"] = "x"
        with self.assertRaises(ConfigError):
            self._load(MINIMAL + """
        folders:
          - name: INBOX
            place: outbox
""")

    def test_folder_text_roundtrip(self):
        raw = parse_folder_text("INBOX\n[Gmail]/Sent Mail :: sent\nOld :: archive :: Pulled/old")
        self.assertEqual(raw, [
            "INBOX",
            {"name": "[Gmail]/Sent Mail", "place": "sent"},
            {"name": "Old", "place": "archive", "label": "Pulled/old"},
        ])
        folders = (
            FolderConfig(name="INBOX"),
            FolderConfig(name="[Gmail]/Sent Mail", place="sent"),
            FolderConfig(name="Old", place="archive", label="Pulled/old"),
        )
        self.assertEqual(parse_folder_text(format_folder_text(folders)), raw)
        # Comma-separated plain names still work (pre-0.3.0 UI syntax).
        self.assertEqual(parse_folder_text("INBOX, Newsletters"), ["INBOX", "Newsletters"])
        with self.assertRaises(ConfigError):
            parse_folder_text("INBOX :: outbox")

    def test_timezone_default_and_validation(self):
        os.environ["TEST_GMAILIFICATION_PW"] = "x"
        cfg = self._load(MINIMAL)
        self.assertEqual(cfg.timezone, "Europe/Brussels")
        cfg = self._load(MINIMAL + "\ntimezone: UTC\n")
        self.assertEqual(cfg.timezone, "UTC")
        with self.assertRaises(ConfigError):
            self._load(MINIMAL + "\ntimezone: Mars/OlympusMons\n")

    def test_throttle_block(self):
        os.environ["TEST_GMAILIFICATION_PW"] = "x"
        cfg = self._load(MINIMAL + "\nthrottle:\n  bandwidth_limit_kbps: 512\n  max_messages_per_cycle: 10\n")
        self.assertEqual(cfg.throttle.bandwidth_limit_kbps, 512)
        self.assertEqual(cfg.throttle.max_messages_per_cycle, 10)
        # Sources without their own block inherit the global values.
        src = cfg.users[0].sources[0]
        self.assertEqual(src.throttle, cfg.throttle)
        self.assertEqual(src.throttle_overrides, ())

    def test_per_source_throttle_override_merges_global(self):
        os.environ["TEST_GMAILIFICATION_PW"] = "x"
        cfg = self._load(MINIMAL + """
        throttle:
          max_messages_per_cycle: 50
""" + "\nthrottle:\n  bandwidth_limit_kbps: 512\n  max_messages_per_cycle: 10\n")
        src = cfg.users[0].sources[0]
        # Overridden field takes the source value; the rest inherit global.
        self.assertEqual(src.throttle.max_messages_per_cycle, 50)
        self.assertEqual(src.throttle.bandwidth_limit_kbps, 512)
        self.assertEqual(src.throttle_overrides, ("max_messages_per_cycle",))
        self.assertEqual(cfg.throttle.max_messages_per_cycle, 10)

    def test_per_source_poll_interval_override(self):
        os.environ["TEST_GMAILIFICATION_PW"] = "x"
        cfg = self._load(MINIMAL + "        poll_interval_seconds: 60\n"
                         + "\npoll_interval_seconds: 300\n")
        src = cfg.users[0].sources[0]
        self.assertEqual(src.poll_interval_seconds, 60)
        self.assertTrue(src.poll_interval_overridden)
        self.assertEqual(cfg.poll_interval_seconds, 300)

    def test_poll_interval_inherits_global(self):
        os.environ["TEST_GMAILIFICATION_PW"] = "x"
        cfg = self._load(MINIMAL + "\npoll_interval_seconds: 300\n")
        src = cfg.users[0].sources[0]
        self.assertEqual(src.poll_interval_seconds, 300)
        self.assertFalse(src.poll_interval_overridden)

    def test_too_small_poll_interval_rejected(self):
        os.environ["TEST_GMAILIFICATION_PW"] = "x"
        with self.assertRaises(ConfigError):
            self._load(MINIMAL + "        poll_interval_seconds: 5\n")

    def test_unknown_throttle_field_rejected(self):
        os.environ["TEST_GMAILIFICATION_PW"] = "x"
        with self.assertRaises(ConfigError):
            self._load(MINIMAL + "\n        throttle:\n          speed: 9\n")


if __name__ == "__main__":
    unittest.main()
