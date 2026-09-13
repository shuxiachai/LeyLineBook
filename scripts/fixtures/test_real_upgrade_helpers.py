"""Pure helpers only: no sockets, application EXEs, UI, or online requests."""

from email.message import Message
from io import BytesIO
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.request
import urllib.response

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
from real_upgrade_smoke import Api, OUTPUT, RejectRedirects, archive_application_log


class MemoryHttp(urllib.request.HTTPHandler):
    def __init__(self, status):
        super().__init__()
        self.status = status
        self.requests = []

    def http_open(self, request):
        self.requests.append(request.full_url)
        headers = Message()
        headers["Content-Type"] = "application/json"
        if 300 <= self.status < 400:
            headers["Location"] = "http://must-not-contact.invalid/token-sink"
        response = urllib.response.addinfourl(BytesIO(b'{"success":true,"data":null}'),
                                             headers, request.full_url, self.status)
        response.msg = "Memory fixture"
        return response


class RealUpgradeHelpersTest(unittest.TestCase):
    def test_all_redirect_statuses_fail_without_a_second_request(self):
        for status in range(300, 400):
            with self.subTest(status=status):
                transport = MemoryHttp(status)
                api = Api(54321, "a" * 64, {"responses": []})
                api.verified = True
                api.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), RejectRedirects(), transport)
                with self.assertRaisesRegex(AssertionError, "Local API redirect rejected"):
                    api.success("/api/state")
                self.assertEqual(transport.requests, ["http://127.0.0.1:54321/api/state"])

    def test_normal_response_still_works(self):
        transport = MemoryHttp(200)
        api = Api(54321, "a" * 64, {"responses": []})
        api.verified = True
        api.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), RejectRedirects(), transport)
        self.assertIsNone(api.success("/api/state"))
        self.assertEqual(transport.requests, ["http://127.0.0.1:54321/api/state"])

    def test_each_stage_retains_only_its_own_application_log(self):
        self.assertEqual(OUTPUT.resolve(), OUTPUT)
        OUTPUT.mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix="real-helper-logs-", dir=OUTPUT))
        self.assertEqual(root.parent, OUTPUT)
        data = root / "data"
        data.mkdir()
        for name, content in (("old", b"http://127.0.0.1:51001\r\n"),
                              ("new", b"http://127.0.0.1:51002\r\n")):
            directory = root / name
            directory.mkdir()
            (data / "task_recorder.log").write_bytes(content)
            archive = archive_application_log(data, directory)
            self.assertEqual(archive.read_bytes(), content)
            self.assertFalse((data / "task_recorder.log").exists())
        self.assertEqual((root / "old/application.log").read_bytes(), b"http://127.0.0.1:51001\r\n")
        self.assertIsNone(archive_application_log(data, root / "new"))

    def test_existing_archive_is_not_overwritten(self):
        self.assertEqual(OUTPUT.resolve(), OUTPUT)
        OUTPUT.mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix="real-helper-preserve-", dir=OUTPUT))
        self.assertEqual(root.parent, OUTPUT)
        data, directory = root / "data", root / "stage"
        data.mkdir()
        directory.mkdir()
        (data / "task_recorder.log").write_bytes(b"new log")
        (directory / "application.log").write_bytes(b"existing evidence")
        with self.assertRaisesRegex(AssertionError, "overwrite evidence"):
            archive_application_log(data, directory)
        self.assertEqual((data / "task_recorder.log").read_bytes(), b"new log")
        self.assertEqual((directory / "application.log").read_bytes(), b"existing evidence")


if __name__ == "__main__":
    unittest.main()
