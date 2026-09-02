import unittest

from gmailification.imap_source import (
    ImapError,
    extract_raw_from_fetch,
    find_special_use,
    parse_list_line,
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


class SpecialUseTest(unittest.TestCase):
    GMAIL_LIST = [
        b'(\\HasNoChildren) "/" "INBOX"',
        b'(\\HasChildren \\Noselect) "/" "[Gmail]"',
        b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Verzonden berichten"',
        b'(\\HasNoChildren \\Trash) "/" "[Gmail]/Prullenbak"',
        b'(\\Drafts \\HasNoChildren) "/" "[Gmail]/Concepten"',
    ]

    def test_parse_list_line_quoted_with_spaces(self):
        attrs, name = parse_list_line(b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"')
        self.assertIn(b"\\Sent", attrs)
        self.assertEqual(name, "[Gmail]/Sent Mail")

    def test_parse_list_line_unquoted(self):
        attrs, name = parse_list_line(b'(\\HasNoChildren) "." INBOX.Sent')
        self.assertEqual(name, "INBOX.Sent")

    def test_parse_list_line_escaped_quote(self):
        _, name = parse_list_line(b'() "/" "odd\\"name"')
        self.assertEqual(name, 'odd"name')

    def test_find_special_use_localized_gmail(self):
        self.assertEqual(find_special_use(self.GMAIL_LIST, "sent"),
                         "[Gmail]/Verzonden berichten")
        self.assertEqual(find_special_use(self.GMAIL_LIST, "trash"),
                         "[Gmail]/Prullenbak")
        self.assertEqual(find_special_use(self.GMAIL_LIST, "drafts"),
                         "[Gmail]/Concepten")
        self.assertIsNone(find_special_use(self.GMAIL_LIST, "archive"))

    def test_find_special_use_case_insensitive_attr(self):
        lines = [b'(\\sent) "/" "Sent Items"']
        self.assertEqual(find_special_use(lines, "sent"), "Sent Items")


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
