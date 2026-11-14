import os
import tempfile
import unittest

from gmailification.alerts import check_and_alert
from gmailification.config import AppConfig, DestinationConfig, SourceConfig, UserConfig
from gmailification.state import Database


class RecordingDest:
    def __init__(self, email):
        self.email = email
        self.alerts = []

    def import_alert(self, subject, body):
        self.alerts.append(subject)


class AlertTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(self.path)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + suffix)
            except FileNotFoundError:
                pass

    def test_deleted_source_does_not_alert(self):
        src = SourceConfig(user="rik", name="telenet", host="h", port=993,
                           username="u", password="p", label="")
        user = UserConfig(name="rik", destination=DestinationConfig("rik@example.com", "t"),
                          sources=(src,))
        cfg = AppConfig(users=(user,), alert_after_hours=0)
        # A live source and one that has since been deleted from the config.
        self.db.record_failure("rik/telenet", "rik", "boom", now=1.0)
        self.db.record_failure("rik/gone", "rik", "boom", now=1.0)
        dest = RecordingDest("rik@example.com")
        check_and_alert(cfg, self.db, {"rik": dest})
        self.assertEqual(len(dest.alerts), 1)
        self.assertIn("rik/telenet", dest.alerts[0])


if __name__ == "__main__":
    unittest.main()
