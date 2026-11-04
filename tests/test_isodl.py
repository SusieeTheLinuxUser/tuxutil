import argparse
import hashlib
import http.server
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tuxutil import isodl


DATA = b"test ISO bytes" * 100


class Handler(http.server.BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(DATA)))
        self.end_headers()

    def do_GET(self):
        if self.path == "/disposition":
            name = '../unsafe\\folder.iso'
        else:
            name = None
        start = 0
        status = 200
        if self.headers.get("Range") and self.path != "/ignore-range":
            start = int(self.headers["Range"].split("=", 1)[1].split("-", 1)[0])
            status = 206
        body = DATA[start:]
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{len(DATA)-1}/{len(DATA)}")
        if name:
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class IsoDownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def run_download(self, target, folder, **kwargs):
        return isodl.cmd_download(argparse.Namespace(target=target, dir=str(folder), output=None,
                                                      sha256=kwargs.get("sha256")))

    def test_resume_and_range_ignored(self):
        for endpoint in ("/resume.iso", "/ignore-range"):
            with self.subTest(endpoint=endpoint), tempfile.TemporaryDirectory() as tmp:
                folder = Path(tmp)
                (folder / (endpoint.rsplit("/", 1)[-1] + ".part")).write_bytes(DATA[:20])
                self.assertEqual(self.run_download(self.base + endpoint, folder,
                                                   sha256=hashlib.sha256(DATA).hexdigest()), 0)
                self.assertEqual((folder / endpoint.rsplit("/", 1)[-1]).read_bytes(), DATA)

    def test_checksum_mismatch_deletes_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            self.assertEqual(self.run_download(self.base + "/bad.iso", folder, sha256="0" * 64), 1)
            self.assertFalse((folder / "bad.iso.part").exists())
            self.assertFalse((folder / "bad.iso").exists())

    def test_content_disposition_is_sanitized_to_bare_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            self.assertEqual(self.run_download(self.base + "/disposition", folder,
                                               sha256=hashlib.sha256(DATA).hexdigest()), 0)
            self.assertEqual((folder / "folder.iso").read_bytes(), DATA)
            self.assertEqual(list(folder.iterdir()), [folder / "folder.iso"])

    def test_file_url_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.run_download("file:///etc/passwd", Path(tmp)), 1)

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(isodl.system, "DRY_RUN", True):
            folder = Path(tmp)
            self.assertEqual(self.run_download(self.base + "/dry.iso", folder), 0)
            self.assertEqual(list(folder.iterdir()), [])

    def test_existing_file_requires_matching_checksum(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            existing = folder / "already.iso"
            existing.write_bytes(DATA)
            self.assertEqual(self.run_download(self.base + "/already.iso", folder,
                                               sha256=hashlib.sha256(DATA).hexdigest()), 0)
            existing.write_bytes(b"keep me")
            self.assertEqual(self.run_download(self.base + "/already.iso", folder,
                                               sha256=hashlib.sha256(DATA).hexdigest()), 1)
            self.assertEqual(existing.read_bytes(), b"keep me")

    def test_catalog_json_contract(self):
        with patch("builtins.print") as output:
            self.assertEqual(isodl.cmd_list(None), 0)
        values = json.loads(output.call_args.args[0])
        self.assertTrue(all(set(row) == {"id", "name", "kind", "direct", "page", "notes"} for row in values))
        windows = next(row for row in values if row["kind"] == "windows")
        self.assertFalse(windows["direct"])


if __name__ == "__main__":
    unittest.main()
