import os
import tempfile
import unittest
from unittest import mock

from gmailification.config import FolderConfig, SourceConfig, ThrottleConfig
from gmailification.state import Database
from gmailification.sync import sync_source


def _msg(n: int) -> bytes:
    return f"Message-ID: <m{n}@test>\r\nSubject: msg {n}\r\n\r\nbody {n}\r\n".encode()


class FakeImap:
    """Stands in for ImapSource: one folder, injectable uid->raw mapping."""

    instances = []

    def __init__(self, cfg, timeout=60):
        self.cfg = cfg
        FakeImap.instances.append(self)
        self.mailbox = FakeImap.mailbox
        self.uidvalidity = FakeImap.uidvalidity

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def status(self, folder):
        uidnext = max(self.mailbox, default=0) + 1
        return self.uidvalidity, uidnext

    def select(self, folder, readonly=True):
        self.selected_readonly = readonly

    def mark_deleted(self, uid):
        assert not self.selected_readonly, "STORE on a read-only folder"
        FakeImap.flagged.append(uid)

    def expunge(self, uids):
        assert not self.selected_readonly, "EXPUNGE on a read-only folder"
        for uid in list(FakeImap.flagged):
            self.mailbox.pop(uid, None)
        FakeImap.expunged.extend(FakeImap.flagged)
        FakeImap.flagged = []

    def uids_after(self, last_uid):
        return sorted(u for u in self.mailbox if u > last_uid)

    def uids_since(self, days):
        return sorted(self.mailbox)

    def fetch_raw(self, uid):
        return self.mailbox[uid]


class FakeDest:
    def __init__(self):
        self.imported = []
        self.calls = []  # (label, kwargs) per import

    def import_raw(self, raw, label, **kwargs):
        self.imported.append(raw)
        self.calls.append((label, kwargs))
        return f"gmail-{len(self.imported)}"


class SyncTest(unittest.TestCase):
    def setUp(self):
        fd, self.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(self.dbpath)
        self.dest = FakeDest()
        self.source = SourceConfig(
            user="rik", name="telenet", host="imap.example.com",
            username="u", password="test-password", label="Pulled/telenet",
        )
        FakeImap.mailbox = {1: _msg(1), 2: _msg(2), 3: _msg(3)}
        FakeImap.uidvalidity = 100
        FakeImap.flagged = []
        FakeImap.expunged = []

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.dbpath + suffix)
            except FileNotFoundError:
                pass

    def _run(self, throttle=None):
        with mock.patch("gmailification.sync.ImapSource", FakeImap):
            return sync_source(self.db, self.source, self.dest, throttle)

    def test_first_run_imports_nothing_but_sets_cursor(self):
        result = self._run()
        self.assertTrue(result.ok)
        self.assertEqual(result.imported, 0)
        st = self.db.get_folder_state("rik/telenet", "INBOX")
        self.assertEqual(st.last_uid, 3)  # pinned to current top

    def test_new_mail_after_first_run_is_imported(self):
        self._run()
        FakeImap.mailbox[4] = _msg(4)
        FakeImap.mailbox[5] = _msg(5)
        result = self._run()
        self.assertEqual(result.imported, 2)
        self.assertEqual(len(self.dest.imported), 2)
        # Rerun: nothing new, no duplicates.
        result = self._run()
        self.assertEqual(result.imported, 0)
        self.assertEqual(len(self.dest.imported), 2)

    def test_uidvalidity_change_rescans_without_duplicates(self):
        self._run()
        FakeImap.mailbox[4] = _msg(4)
        self._run()  # imports msg 4
        # Server rebuilds the mailbox: same messages, new UIDs, new UIDVALIDITY.
        FakeImap.uidvalidity = 200
        FakeImap.mailbox = {10: _msg(1), 11: _msg(2), 12: _msg(3), 13: _msg(4), 14: _msg(5)}
        result = self._run()
        self.assertTrue(result.ok)
        # Only msg 5 is genuinely new; 1-3 predate the cursor but were never
        # imported (first-run policy), so the rescan imports them too — the
        # dedupe table only guards msg 4 here.
        self.assertIn(_msg(5), self.dest.imported)
        self.assertEqual(self.dest.imported.count(_msg(4)), 1)

    def test_backfill_days_on_first_run(self):
        self.source = SourceConfig(
            user="rik", name="telenet", host="h", username="u", password="test-password",
            label="Pulled/telenet", backfill_days=7,
        )
        result = self._run()
        self.assertEqual(result.imported, 3)

    def test_per_cycle_message_cap(self):
        self.source = SourceConfig(
            user="rik", name="telenet", host="h", username="u", password="test-password",
            label="Pulled/telenet", backfill_days=7,
        )
        throttle = ThrottleConfig(max_messages_per_cycle=2)
        result = self._run(throttle)
        self.assertEqual(result.imported, 2)
        # Next cycle picks up the remainder.
        result = self._run(throttle)
        self.assertEqual(result.imported, 1)

    def test_poll_history_written_on_success_and_failure(self):
        self.source = SourceConfig(
            user="rik", name="telenet", host="h", username="u", password="test-password",
            label="Pulled/telenet", backfill_days=7,
        )
        self._run()
        polls = self.db.history_since(0)
        self.assertEqual(len(polls), 1)
        self.assertTrue(polls[0].ok)
        self.assertEqual(polls[0].imported, 3)

        def boom(uid):
            raise RuntimeError("nope")

        FakeImap.mailbox[9] = _msg(9)
        with mock.patch.object(FakeImap, "fetch_raw", side_effect=boom):
            with mock.patch("gmailification.sync.ImapSource", FakeImap):
                sync_source(self.db, self.source, self.dest)
        polls = self.db.history_since(0)
        self.assertEqual(len(polls), 2)
        self.assertFalse(polls[-1].ok)
        self.assertIn("nope", polls[-1].error)

    def test_failure_recorded_and_isolated(self):
        def boom(uid):
            raise RuntimeError("disk on fire")

        self._run()
        FakeImap.mailbox[4] = _msg(4)
        with mock.patch.object(FakeImap, "fetch_raw", side_effect=boom):
            with mock.patch("gmailification.sync.ImapSource", FakeImap):
                result = sync_source(self.db, self.source, self.dest)
        self.assertFalse(result.ok)
        st = [s for s in self.db.all_statuses() if s.source_key == "rik/telenet"][0]
        self.assertEqual(st.consecutive_failures, 1)
        self.assertIn("disk on fire", st.last_error)
        # Recovery on the next good run.
        result = self._run()
        self.assertTrue(result.ok)
        self.assertEqual(result.imported, 1)

    def test_inbox_folder_flags(self):
        self.source = SourceConfig(
            user="rik", name="telenet", host="h", username="u", password="test-password",
            label="Pulled/telenet", backfill_days=7,
        )
        self._run()
        label, kwargs = self.dest.calls[0]
        self.assertEqual(label, "Pulled/telenet")
        self.assertEqual(kwargs, {"inbox": True, "unread": True, "sent": False})

    def test_sent_folder_flags_and_label_override(self):
        self.source = SourceConfig(
            user="rik", name="telenet", host="h", username="u", password="test-password",
            label="Pulled/telenet", backfill_days=7,
            folders=(FolderConfig(name="Sent", place="sent", label="Pulled/telenet/sent"),),
        )
        result = self._run()
        self.assertEqual(result.imported, 3)
        label, kwargs = self.dest.calls[0]
        self.assertEqual(label, "Pulled/telenet/sent")
        self.assertEqual(kwargs, {"inbox": False, "unread": False, "sent": True})

    def test_archive_folder_flags(self):
        self.source = SourceConfig(
            user="rik", name="telenet", host="h", username="u", password="test-password",
            label="Pulled/telenet", backfill_days=7,
            folders=(FolderConfig(name="Old", place="archive"),),
        )
        self._run()
        label, kwargs = self.dest.calls[0]
        self.assertEqual(label, "Pulled/telenet")  # no override -> source label
        self.assertEqual(kwargs, {"inbox": False, "unread": False, "sent": False})

    def test_keep_mode_never_deletes(self):
        self._run()
        FakeImap.mailbox[4] = _msg(4)
        result = self._run()
        self.assertEqual(result.imported, 1)
        self.assertEqual(result.deleted, 0)
        self.assertEqual(FakeImap.expunged, [])
        self.assertEqual(sorted(FakeImap.mailbox), [1, 2, 3, 4])

    def test_delete_mode_moves_only_transferred_messages(self):
        self.source = SourceConfig(
            user="rik", name="telenet", host="h", username="u", password="test-password",
            label="Pulled/telenet", backfill_days=7, after_import="delete",
        )
        result = self._run()
        self.assertEqual(result.imported, 3)
        self.assertEqual(result.deleted, 3)
        self.assertEqual(FakeImap.mailbox, {})  # source drained
        # New mail also gets moved on later cycles.
        FakeImap.mailbox[4] = _msg(4)
        result = self._run()
        self.assertEqual((result.imported, result.deleted), (1, 1))
        self.assertEqual(FakeImap.mailbox, {})

    def test_delete_mode_spares_failed_imports(self):
        self.source = SourceConfig(
            user="rik", name="telenet", host="h", username="u", password="test-password",
            label="Pulled/telenet", backfill_days=7, after_import="delete",
        )

        class RejectingDest:
            def __init__(self):
                self.calls = 0

            def import_raw(self, raw, label, **kwargs):
                self.calls += 1
                if raw == _msg(2):
                    raise ValueError("API says no")
                return f"gmail-{self.calls}"

        self.dest = RejectingDest()
        result = self._run()
        self.assertTrue(result.ok)
        self.assertEqual(result.imported, 2)
        self.assertEqual(result.deleted, 2)
        # The rejected message stays in the source, untouched.
        self.assertEqual(sorted(FakeImap.mailbox), [2])

    def test_delete_mode_deletes_dupes_already_in_destination(self):
        # Import msg 1-3 in keep mode first...
        keep_source = self.source
        self.source = SourceConfig(
            user="rik", name="telenet", host="h", username="u", password="test-password",
            label="Pulled/telenet", backfill_days=7,
        )
        self._run()
        self.assertEqual(len(self.dest.imported), 3)
        # ...then switch the source to delete mode and force a rescan.
        FakeImap.uidvalidity = 200
        FakeImap.mailbox = {10: _msg(1), 11: _msg(2), 12: _msg(3)}
        self.source = SourceConfig(
            user="rik", name="telenet", host="h", username="u", password="test-password",
            label="Pulled/telenet", backfill_days=7, after_import="delete",
        )
        result = self._run()
        self.assertEqual(result.imported, 0)  # all dupes
        self.assertEqual(result.skipped_dupes, 3)
        self.assertEqual(result.deleted, 3)   # but moved out of the source
        self.assertEqual(FakeImap.mailbox, {})
        self.assertEqual(len(self.dest.imported), 3)  # no double import
        del keep_source


if __name__ == "__main__":
    unittest.main()
