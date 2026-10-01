import os
import tempfile
import time
import unittest
from unittest import mock

from gmailification.config import FolderConfig, SourceConfig, ThrottleConfig
from gmailification.state import MAX_RETRY_ATTEMPTS, Database
from gmailification.sync import sync_source
from gmailification.util import TransientError


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

    def resolve(self, name):
        return FakeImap.resolutions.get(name, name)

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
        FakeImap.expunge_calls.append(list(uids))
        for uid in list(FakeImap.flagged):
            self.mailbox.pop(uid, None)
        FakeImap.expunged.extend(FakeImap.flagged)
        FakeImap.flagged = []

    def uids_after(self, last_uid):
        return sorted(u for u in self.mailbox if u > last_uid)

    def uids_since(self, days):
        return sorted(self.mailbox)

    def uids_received_since(self, ts, lo, hi):
        # Day granularity, like IMAP SINCE; unknown arrival = just now.
        day = ts - ts % 86400
        return sorted(u for u in self.mailbox
                      if lo <= u <= hi and FakeImap.arrived.get(u, time.time()) >= day)

    def internal_dates(self, uids):
        return {u: FakeImap.arrived.get(u, time.time()) for u in uids}

    def existing_uids(self, uids):
        return sorted(u for u in uids if u in self.mailbox)

    def fetch_raw(self, uid):
        FakeImap.fetched.append(uid)
        return self.mailbox[uid]


class FakeDest:
    def __init__(self):
        self.imported = []
        self.calls = []  # (label, kwargs) per import

    def import_raw(self, raw, label, **kwargs):
        self.imported.append(raw)
        self.calls.append((label, kwargs))
        return f"gmail-{len(self.imported)}"


class _SyncBase(unittest.TestCase):
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
        FakeImap.resolutions = {}
        FakeImap.arrived = {}
        FakeImap.fetched = []
        FakeImap.expunge_calls = []

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.dbpath + suffix)
            except FileNotFoundError:
                pass

    def _run(self, throttle=None):
        with mock.patch("gmailification.sync.ImapSource", FakeImap):
            return sync_source(self.db, self.source, self.dest, throttle)


class SyncTest(_SyncBase):
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

    def test_auto_folder_resolves_and_keys_state_by_real_name(self):
        FakeImap.resolutions = {"auto:sent": "[Gmail]/Verzonden berichten"}
        self.source = SourceConfig(
            user="rik", name="telenet", host="h", username="u", password="test-password",
            label="Pulled/telenet", backfill_days=7,
            folders=(FolderConfig(name="auto:sent", place="sent"),),
        )
        result = self._run()
        self.assertEqual(result.imported, 3)
        # State cursor lives under the resolved name, so a later switch
        # between literal and auto: forms keeps the same cursor.
        st = self.db.get_folder_state("rik/telenet", "[Gmail]/Verzonden berichten")
        self.assertIsNotNone(st)
        self.assertIsNone(self.db.get_folder_state("rik/telenet", "auto:sent"))

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



class FlakyDest(FakeDest):
    """Rejects the messages in `bad` (non-transiently) until fixed."""

    def __init__(self, bad):
        super().__init__()
        self.bad = set(bad)

    def import_raw(self, raw, label, **kwargs):
        if raw in self.bad:
            raise UnicodeEncodeError("ascii", "x", 0, 1, "ordinal not in range(128)")
        return super().import_raw(raw, label, **kwargs)


class ResilienceTest(_SyncBase):
    """Failed imports and leftovers must eventually be retried and moved."""

    def setUp(self):
        super().setUp()
        self.source = SourceConfig(
            user="rik", name="telenet", host="h", username="u", password="test-password",
            label="Pulled/telenet", after_import="delete",
        )

    def _age_retries(self, seconds):
        with self.db._conn() as conn:
            conn.execute("UPDATE retry_queue SET next_attempt_at = next_attempt_at - ?"
                         " WHERE next_attempt_at IS NOT NULL", (seconds,))

    def test_failed_import_is_retried_and_then_moved(self):
        self._run()  # first run: cursor at 3
        FakeImap.mailbox.update({4: _msg(4), 5: _msg(5)})
        self.dest = FlakyDest({_msg(4)})
        result = self._run()
        self.assertEqual((result.imported, result.deleted), (1, 1))
        self.assertIn(4, FakeImap.mailbox)  # failed one stays in the source
        [entry] = self.db.retry_entries("rik/telenet")
        self.assertEqual((entry.uid, entry.attempts), (4, 1))
        self.assertIn("UnicodeEncodeError", entry.last_error)

        # Not due yet: no refetch on the next poll.
        FakeImap.fetched = []
        self._run()
        self.assertNotIn(4, FakeImap.fetched)

        # Once due and the cause is fixed, it is imported and moved.
        self.dest.bad.clear()
        self._age_retries(3600)
        result = self._run()
        self.assertEqual((result.imported, result.deleted), (1, 1))
        self.assertNotIn(4, FakeImap.mailbox)
        self.assertEqual(self.db.retry_entries(), [])

    def test_retry_gives_up_after_max_attempts(self):
        self._run()
        FakeImap.mailbox[4] = _msg(4)
        self.dest = FlakyDest({_msg(4)})
        self._run()
        for _ in range(MAX_RETRY_ATTEMPTS + 2):
            self._age_retries(10 * 86400)
            self._run()
        [entry] = self.db.retry_entries()
        self.assertEqual(entry.attempts, MAX_RETRY_ATTEMPTS)
        self.assertIsNone(entry.next_attempt_at)
        # A manual requeue makes it due again.
        self.assertEqual(self.db.requeue("rik/telenet"), 1)
        self.dest.bad.clear()
        self._run()
        self.assertNotIn(4, FakeImap.mailbox)

    def test_retry_for_vanished_message_is_dropped(self):
        self._run()
        FakeImap.mailbox[4] = _msg(4)
        self.dest = FlakyDest({_msg(4)})
        self._run()
        del FakeImap.mailbox[4]  # user deleted it by hand
        self._age_retries(3600)
        result = self._run()
        self.assertTrue(result.ok)
        self.assertEqual(self.db.retry_entries(), [])

    def test_eager_expunge_survives_connection_drop_mid_batch(self):
        self._run()
        FakeImap.mailbox.update({4: _msg(4), 5: _msg(5), 6: _msg(6)})

        class DropAfterTwo(FakeDest):
            def import_raw(self, raw, label, **kwargs):
                if len(self.imported) == 2:
                    raise TransientError("router rebooting")
                return super().import_raw(raw, label, **kwargs)

        self.dest = DropAfterTwo()
        with mock.patch("gmailification.util.time.sleep"):
            result = self._run()
        self.assertFalse(result.ok)
        # The two that reached Gmail are already gone from the source.
        self.assertEqual(sorted(FakeImap.mailbox), [1, 2, 3, 6])
        self.assertIn([4], FakeImap.expunge_calls)
        self.dest = FakeDest()
        result = self._run()
        self.assertEqual((result.imported, result.deleted), (1, 1))
        self.assertEqual(sorted(FakeImap.mailbox), [1, 2, 3])

    def test_sweep_derives_floor_for_pre_0_8_state(self):
        # Mail 1-3 arrived earlier on the day of setup; the folder state comes
        # from a version that did not record the first-run cursor.
        setup = time.time()
        FakeImap.arrived = {1: setup - 60, 2: setup - 60, 3: setup - 60}
        self._run()
        with self.db._conn() as conn:
            conn.execute("UPDATE folder_state SET watch_from_uid = NULL, last_sweep_at = NULL,"
                         " watch_since = ?", (setup,))
        FakeImap.mailbox[4] = _msg(4)
        FakeImap.arrived[4] = setup + 60
        self.db.set_folder_state("rik/telenet", "INBOX", 100, 4)  # 4 was skipped
        result = self._run()
        self.assertEqual(result.imported, 1)
        self.assertEqual(sorted(FakeImap.mailbox), [1, 2, 3])
        self.assertEqual(self.db.get_folder_state("rik/telenet", "INBOX").watch_from_uid, 3)

    def test_sweep_spares_same_day_mail_from_before_setup(self):
        # 1-3 arrived earlier today, before the source was configured.
        self._run()
        self.assertEqual(self.db.get_folder_state("rik/telenet", "INBOX").watch_from_uid, 3)
        with self.db._conn() as conn:
            conn.execute("UPDATE folder_state SET last_sweep_at = NULL")
        self._run()
        self.assertEqual(sorted(FakeImap.mailbox), [1, 2, 3])
        self.assertEqual(self.db.retry_entries(), [])

    def test_sweep_moves_leftovers_but_not_preexisting_mail(self):
        # Mail 1-3 predates the source being set up (a week old); the folder
        # state comes from an older version without a first-run cursor.
        week_ago = time.time() - 7 * 86400
        FakeImap.arrived = {1: week_ago, 2: week_ago, 3: week_ago}
        self._run()
        with self.db._conn() as conn:
            conn.execute("UPDATE folder_state SET watch_from_uid = NULL")
        # Leftovers from before this version: msg 4 failed (recorded only in
        # the dedupe table, never queued), msg 5 was imported and flagged but
        # its EXPUNGE was lost.
        FakeImap.mailbox.update({4: _msg(4), 5: _msg(5)})
        self.db.record_import("rik", "mid:m4@test", "rik/telenet", None, status="failed_permanent")
        self.db.record_import("rik", "mid:m5@test", "rik/telenet", "gmail-x")
        self.db.set_folder_state("rik/telenet", "INBOX", 100, 5)
        with self.db._conn() as conn:
            conn.execute("UPDATE folder_state SET last_sweep_at = NULL")
        result = self._run()
        self.assertTrue(result.ok)
        self.assertEqual(self.dest.imported, [_msg(4)])  # 5 was already there
        self.assertEqual(sorted(FakeImap.mailbox), [1, 2, 3])  # old mail untouched
        self.assertEqual(self.db.retry_entries(), [])

    def test_sweep_does_not_resurrect_given_up_messages(self):
        self._run()
        FakeImap.mailbox[4] = _msg(4)
        self.dest = FlakyDest({_msg(4)})
        self._run()
        self.db.record_retry_failure("rik/telenet", "INBOX", 100, 4, "nope", give_up=True)
        with self.db._conn() as conn:
            conn.execute("UPDATE folder_state SET last_sweep_at = NULL")
        FakeImap.fetched = []
        self._run()
        self.assertNotIn(4, FakeImap.fetched)

    def test_failed_message_still_imported_via_other_source(self):
        # A failure recorded in the dedupe table must not block the same
        # message (same Message-ID) arriving through another path.
        self.db.record_import("rik", "mid:m4@test", "rik/other", None, status="failed_permanent")
        self._run()
        FakeImap.mailbox[4] = _msg(4)
        result = self._run()
        self.assertEqual((result.imported, result.deleted), (1, 1))
        self.assertTrue(self.db.is_imported("rik", "mid:m4@test"))


if __name__ == "__main__":
    unittest.main()
