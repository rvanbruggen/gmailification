import os
import tempfile
import unittest

from gmailification.config import ConfigError, load_config

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
        os.environ["TEST_GMAILIFICATION_PW"] = "s3cret"
        cfg = self._load(MINIMAL)
        self.assertEqual(len(cfg.users), 1)
        src = cfg.users[0].sources[0]
        self.assertEqual(src.key, "rik/telenet")
        self.assertEqual(src.password, "s3cret")
        self.assertEqual(src.label, "Pulled/telenet")  # defaulted from name
        self.assertEqual(src.folders, ("INBOX",))
        self.assertEqual(cfg.poll_interval_seconds, 180)
        self.assertEqual(cfg.throttle.max_messages_per_cycle, 200)

    def test_missing_env_var(self):
        os.environ.pop("TEST_GMAILIFICATION_PW", None)
        with self.assertRaises(ConfigError) as ctx:
            self._load(MINIMAL)
        self.assertIn("TEST_GMAILIFICATION_PW", str(ctx.exception))

    def test_password_file(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as fh:
            fh.write("filepw\n")
            pw_path = fh.name
        try:
            cfg = self._load(MINIMAL.replace(
                "password_env: TEST_GMAILIFICATION_PW", f"password_file: {pw_path}"))
            self.assertEqual(cfg.users[0].sources[0].password, "filepw")
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

    def test_throttle_block(self):
        os.environ["TEST_GMAILIFICATION_PW"] = "x"
        cfg = self._load(MINIMAL + "\nthrottle:\n  bandwidth_limit_kbps: 512\n  max_messages_per_cycle: 10\n")
        self.assertEqual(cfg.throttle.bandwidth_limit_kbps, 512)
        self.assertEqual(cfg.throttle.max_messages_per_cycle, 10)


if __name__ == "__main__":
    unittest.main()
