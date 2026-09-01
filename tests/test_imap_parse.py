import unittest

from gmailification.imap_source import (
    ImapError,
    extract_raw_from_fetch,
    parse_search_uids,
    parse_status,
)
from gmailification.util import dedupe_key


class ImapParseTest(unittest.TestCase):
    def test_extract_raw_typical(self):
        data = [(b"1 (UID 456 BODY[] {14}", b"raw email body"), b")"]
        self.assertEqual(extract_raw_from_fetch(data), b"raw email body")

    def test_extract_raw_with_flags(self):
        data = [
            (b"7 (FLAGS (\\Seen) UID 9 BODY[] {3}", b"abc"),
            b" FLAGS (\\Seen))",
        ]
        self.assertEqual(extract_raw_from_fetch(data), b"abc")

    def test_extract_raw_empty_raises(self):
        with self.assertRaises(ImapError):
            extract_raw_from_fetch([None])

    def test_parse_search_uids(self):
        self.assertEqual(parse_search_uids([b"3 1 2"]), [1, 2, 3])
        self.assertEqual(parse_search_uids([b""]), [])
        self.assertEqual(parse_search_uids([None]), [])

    def test_parse_status(self):
        line = b'"INBOX" (UIDVALIDITY 1234 UIDNEXT 5678)'
        self.assertEqual(parse_status(line), (1234, 5678))
        with self.assertRaises(ImapError):
            parse_status(b'"INBOX" (MESSAGES 3)')


class DedupeKeyTest(unittest.TestCase):
    def test_message_id_used(self):
        raw = b"From: a@b.c\r\nMessage-ID: <xyz@host>\r\nSubject: hi\r\n\r\nbody\r\n"
        self.assertEqual(dedupe_key(raw), "mid:xyz@host")

    def test_missing_message_id_hashes_content(self):
        raw = b"From: a@b.c\r\nSubject: hi\r\n\r\nbody\r\n"
        key = dedupe_key(raw)
        self.assertTrue(key.startswith("sha256:"))
        self.assertEqual(key, dedupe_key(raw))
        self.assertNotEqual(key, dedupe_key(raw + b"x"))

    def test_same_message_id_same_key(self):
        a = b"Message-ID: <same@id>\r\nSubject: one\r\n\r\nbody1\r\n"
        b = b"Message-ID: <same@id>\r\nSubject: two\r\n\r\nbody2\r\n"
        self.assertEqual(dedupe_key(a), dedupe_key(b))


if __name__ == "__main__":
    unittest.main()
