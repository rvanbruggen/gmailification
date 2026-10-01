import base64
import json
import os
import tempfile
import unittest
from unittest import mock

import httplib2
from googleapiclient.discovery import build
from googleapiclient.http import HttpMock

from gmailification.gmail_dest import GmailDestination
from gmailification.util import TransientError

EIGHT_BIT = ("From: José <a@example.com>\r\nSubject: café\r\n"
             "Content-Type: text/plain; charset=utf-8\r\n"
             "Content-Transfer-Encoding: 8bit\r\n\r\nhéllo\r\n").encode("utf-8")


class _CapturingHttp(HttpMock):
    def request(self, uri, method="GET", body=None, headers=None, **kwargs):
        self.sent = (uri, body)
        return super().request(uri, method, body, headers, **kwargs)


class GmailDestTest(unittest.TestCase):
    def setUp(self):
        self.dest = GmailDestination("rik", "rik@example.com", "/nonexistent")
        self.dest._label_ids["Pulled/x"] = "Label_1"
        fd, self.resp = tempfile.mkstemp()
        os.close(fd)
        with open(self.resp, "w") as fh:
            json.dump({"id": "gm1"}, fh)
        self.http = _CapturingHttp(self.resp, {"status": "200"})
        svc = build("gmail", "v1", http=self.http, static_discovery=True)
        self.patch = mock.patch.object(GmailDestination, "_service", return_value=svc)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        os.unlink(self.resp)

    def test_8bit_message_is_imported_byte_for_byte(self):
        # Used to fail with "'ascii' codec can't encode characters".
        self.assertEqual(self.dest.import_raw(EIGHT_BIT, "Pulled/x"), "gm1")
        uri, body = self.http.sent
        payload = json.loads(body)
        self.assertEqual(base64.urlsafe_b64decode(payload["raw"]), EIGHT_BIT)
        self.assertEqual(set(payload["labelIds"]), {"Label_1", "INBOX", "UNREAD"})
        self.assertIn("internalDateSource=dateHeader", uri)

    def test_ascii_message_keeps_media_upload(self):
        raw = b"From: a@example.com\r\nSubject: hi\r\n\r\nbody\r\n"
        self.assertEqual(self.dest.import_raw(raw, "Pulled/x"), "gm1")
        uri, body = self.http.sent
        self.assertIn("uploadType=multipart", uri)
        self.assertIn(raw, body)

    def test_dns_failure_is_transient(self):
        def boom():
            raise httplib2.ServerNotFoundError("Unable to find the server at gmail.googleapis.com")
        with self.assertRaises(TransientError):
            self.dest._call(boom)


if __name__ == "__main__":
    unittest.main()
