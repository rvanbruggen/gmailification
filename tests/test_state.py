import os
import sqlite3
import tempfile
import unittest

from gmailification.state import Database


class StateTest(unittest.TestCase):
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

    def test_folder_state_roundtrip(self):
        self.assertIsNone(self.db.get_folder_state("rik/telenet", "INBOX"))
        self.db.set_folder_state("rik/telenet", "INBOX", 42, 100)
        st = self.db.get_folder_state("rik/telenet", "INBOX")
        self.assertEqual((st.uidvalidity, st.last_uid), (42, 100))
        self.db.set_folder_state("rik/telenet", "INBOX", 43, 5)
        st = self.db.get_folder_state("rik/telenet", "INBOX")
        self.assertEqual((st.uidvalidity, st.last_uid), (43, 5))

    def test_dedupe_is_per_user(self):
        self.db.record_import("rik", "mid:abc", "rik/telenet", "gm1")
        self.assertTrue(self.db.is_imported("rik", "mid:abc"))
        self.assertFalse(self.db.is_imported("mark", "mid:abc"))
        # Idempotent re-record does not blow up.
        self.db.record_import("rik", "mid:abc", "rik/trol", "gm2")

    def test_alert_thresholds(self):
        now = 1_000_000.0
        h = 3600
        self.db.record_failure("rik/telenet", "rik", "boom", now=now)
        # Not failing long enough yet.
        self.assertEqual(self.db.statuses_needing_alert(6, 24, now=now + 5 * h), [])
        due = self.db.statuses_needing_alert(6, 24, now=now + 7 * h)
        self.assertEqual([s.source_key for s in due], ["rik/telenet"])
        self.db.mark_alerted("rik/telenet", now=now + 7 * h)
        # No re-alert within realert window...
        self.assertEqual(self.db.statuses_needing_alert(6, 24, now=now + 20 * h), [])
        # ...but again after it.
        self.assertEqual(len(self.db.statuses_needing_alert(6, 24, now=now + 32 * h)), 1)
        # Success clears everything.
        self.db.record_success("rik/telenet", "rik", now=now + 33 * h)
        self.assertEqual(self.db.statuses_needing_alert(6, 24, now=now + 40 * h), [])
        st = self.db.all_statuses()[0]
        self.assertIsNone(st.failing_since)
        self.assertEqual(st.consecutive_failures, 0)
        self.assertEqual(st.total_failure, 1)
        self.assertEqual(st.total_success, 1)

    def test_poll_history_roundtrip_and_prune(self):
        t0 = 1_000_000.0
        self.db.record_poll("rik/telenet", "rik", ok=True, imported=2, duration=1.5, now=t0)
        self.db.record_poll("rik/telenet", "rik", ok=True, now=t0 + 180)
        self.db.record_poll("rik/trol", "rik", ok=False, error="boom", now=t0 + 200)
        rows = self.db.history_since(0)
        self.assertEqual(len(rows), 3)
        self.assertEqual([r.ts for r in rows], sorted(r.ts for r in rows))
        only_telenet = self.db.history_since(0, "rik/telenet")
        self.assertEqual(len(only_telenet), 2)
        self.assertEqual(only_telenet[0].imported, 2)
        # since filter
        self.assertEqual(len(self.db.history_since(t0 + 190)), 1)
        # prune keeps recent, drops old: cutoff lands between t0+180 and t0+200
        removed = self.db.prune_history(days=1, now=t0 + 190 + 86400)
        self.assertEqual(removed, 2)
        self.assertEqual(len(self.db.history_since(0)), 1)

    def test_recent_events_only_noteworthy(self):
        t0 = 1_000_000.0
        self.db.record_poll("rik/telenet", "rik", ok=True, now=t0)          # quiet
        self.db.record_poll("rik/telenet", "rik", ok=True, imported=3, now=t0 + 1)
        self.db.record_poll("rik/telenet", "rik", ok=False, error="x", now=t0 + 2)
        self.db.record_poll("rik/trol", "rik", ok=True, deleted=1, now=t0 + 3)
        events = self.db.recent_events(limit=10)
        self.assertEqual(len(events), 3)  # the quiet poll is excluded
        self.assertEqual(events[0].ts, t0 + 3)  # newest first
        only = self.db.recent_events(limit=10, source_key="rik/telenet")
        self.assertEqual(len(only), 2)

    def test_failing_since_sticks_to_first_failure(self):
        self.db.record_failure("rik/telenet", "rik", "a", now=100.0)
        self.db.record_failure("rik/telenet", "rik", "b", now=200.0)
        st = self.db.all_statuses()[0]
        self.assertEqual(st.failing_since, 100.0)
        self.assertEqual(st.consecutive_failures, 2)
        self.assertEqual(st.last_error, "b")


    def test_failed_import_is_not_delivered_and_can_be_upgraded(self):
        self.db.record_import("rik", "mid:x", "rik/telenet", None, status="failed_permanent")
        self.assertFalse(self.db.is_imported("rik", "mid:x"))
        self.db.record_import("rik", "mid:x", "rik/telenet", "gm1")
        self.assertTrue(self.db.is_imported("rik", "mid:x"))
        # ...but a later failure never downgrades a real import.
        self.db.record_import("rik", "mid:x", "rik/trol", None, status="failed_permanent")
        self.assertTrue(self.db.is_imported("rik", "mid:x"))

    def test_retry_queue_backoff(self):
        self.db.record_retry_failure("rik/telenet", "INBOX", 1, 7, "boom", now=1000)
        self.assertEqual(self.db.due_retries("rik/telenet", "INBOX", now=1000), [])
        self.assertEqual(self.db.due_retries("rik/telenet", "INBOX", now=1000 + 900), [7])
        self.db.record_retry_failure("rik/telenet", "INBOX", 1, 7, "boom", now=2000)
        self.assertEqual(self.db.due_retries("rik/telenet", "INBOX", now=2000 + 900), [])
        self.assertEqual(self.db.due_retries("rik/telenet", "INBOX", now=2000 + 3600), [7])
        # Queueing again (e.g. by a sweep) keeps the existing schedule.
        self.db.queue_retry("rik/telenet", "INBOX", 1, 7, now=2001)
        self.assertEqual(self.db.retry_entries()[0].attempts, 2)
        self.assertEqual(self.db.drop_stale_retries("rik/telenet", "INBOX", 2), 1)

    def test_migrates_pre_0_8_folder_state(self):
        os.unlink(self.path)
        conn = sqlite3.connect(self.path)
        conn.executescript("""
            CREATE TABLE folder_state (source_key TEXT NOT NULL, folder TEXT NOT NULL,
                uidvalidity INTEGER NOT NULL, last_uid INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL, PRIMARY KEY (source_key, folder));
            CREATE TABLE imported_messages (user TEXT NOT NULL, dedupe_key TEXT NOT NULL,
                source_key TEXT NOT NULL, gmail_id TEXT,
                status TEXT NOT NULL DEFAULT 'imported', imported_at REAL NOT NULL,
                PRIMARY KEY (user, dedupe_key));
            INSERT INTO folder_state VALUES ('rik/a', 'INBOX', 1, 50, 9000);
            INSERT INTO folder_state VALUES ('rik/b', 'INBOX', 1, 60, 9500);
            INSERT INTO imported_messages VALUES ('rik', 'mid:1', 'rik/a', 'g', 'imported', 5000);
            INSERT INTO imported_messages VALUES ('rik', 'mid:2', 'rik/a', 'g', 'imported', 6000);
        """)
        conn.commit()
        conn.close()
        db = Database(self.path)
        a = db.get_folder_state("rik/a", "INBOX")
        self.assertEqual((a.last_uid, a.watch_since, a.watch_from_uid), (50, 5000, None))
        self.assertEqual(db.get_folder_state("rik/b", "INBOX").watch_since, 9500)
        Database(self.path)  # second open: migration is idempotent


if __name__ == "__main__":
    unittest.main()
