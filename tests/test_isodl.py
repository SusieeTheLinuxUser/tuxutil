import argparse
import contextlib
import fcntl
import hashlib
import http.server
import io
import json
import os
import socket
import tempfile
import threading
import unittest
import urllib.error
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
        redirects = {"/to-password": "http://alice:hunter2@example.test:80/a.iso", "/to-ftp": "ftp://example.test/a.iso", "/moved": "/moved.iso"}
        if self.path in redirects:
            self.send_response(302)
            self.send_header("Location", redirects[self.path])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/SUMS":
            self.send_response(200)
            self.send_header("Content-Length", str(len(self.sums)))
            self.end_headers()
            self.wfile.write(self.sums)
            return
        if self.path == "/disposition":
            name = '../unsafe\\folder.iso'
        else:
            name = None
        start = 0
        status = 200
        if self.headers.get("Range") and self.path != "/ignore-range":
            start = int(self.headers["Range"].split("=", 1)[1].split("-", 1)[0])
            status = 206
        if start >= len(DATA):
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{len(DATA)}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
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
    def test_checksum_list_gives_the_newest_whatever_the_file_order(self):
        names = ["Zorin-OS-18-Core-64-bit-r3.iso", "Zorin-OS-18-Core-64-bit.iso", "Zorin-OS-18.1-Core-64-bit-r1.iso",
                 "Zorin-OS-18.1-Core-64-bit.iso", "Zorin-OS-18.1-Lite-64-bit.iso"]
        lines = [f"{i:064x}  {name}\n".encode() for i, name in enumerate(names)]
        pattern = r"Zorin-OS-\d+(\.\d+)?-Core-64-bit(-r\d+)?\.iso"
        with patch.object(isodl, "_request") as request:
            request.return_value.__enter__.return_value = lines
            self.assertEqual(isodl._from_sums("https://x.test/sums", pattern), (names[2], f"{2:064x}"))

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
        return isodl.cmd_download(argparse.Namespace(target=target, dir=str(folder), output=kwargs.get("output"),
                                                      sha256=kwargs.get("sha256")))

    def test_resume_and_range_ignored(self):
        for endpoint in ("/resume.iso", "/ignore-range"):
            with self.subTest(endpoint=endpoint), tempfile.TemporaryDirectory() as tmp:
                folder = Path(tmp)
                (folder / (endpoint.rsplit("/", 1)[-1] + ".part")).write_bytes(DATA[:20])
                self.assertEqual(self.run_download(self.base + endpoint, folder,
                                                   sha256=hashlib.sha256(DATA).hexdigest()), 0)
                self.assertEqual((folder / endpoint.rsplit("/", 1)[-1]).read_bytes(), DATA)

    def test_finished_part_is_not_stuck_on_range_error(self):
        """A kill during the checksum step leaves a whole .part, and the server answers its Range with 416."""
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "whole.iso.part").write_bytes(DATA)
            self.assertEqual(self.run_download(self.base + "/whole.iso", folder,
                                               sha256=hashlib.sha256(DATA).hexdigest()), 0)
            self.assertEqual((folder / "whole.iso").read_bytes(), DATA)

    def test_part_larger_than_the_file_starts_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "stale.iso.part").write_bytes(DATA + b"old version")
            self.assertEqual(self.run_download(self.base + "/stale.iso", folder,
                                               sha256=hashlib.sha256(DATA).hexdigest()), 0)
            self.assertEqual((folder / "stale.iso").read_bytes(), DATA)

    def test_catalog_download_uses_the_newest_listed_file_and_its_checksum(self):
        sums = (f"{'1' * 64} *distro-1.0.iso\n{hashlib.sha256(DATA).hexdigest()} *distro-1.1.iso\n"
                f"{'2' * 64} *distro-server-1.1.iso\n")
        entry = {"id": "d", "name": "D", "direct": True, "page": "p", "checksum_url": self.base + "/SUMS",
                 "pattern": r"distro-[\d.]+\.iso"}
        with tempfile.TemporaryDirectory() as tmp, patch.object(isodl, "CATALOG", [entry]), \
                patch.object(Handler, "sums", sums.encode(), create=True):
            folder = Path(tmp)
            self.assertEqual(self.run_download("d", folder), 0)
            self.assertEqual((folder / "distro-1.1.iso").read_bytes(), DATA)

    def latest_with_body(self, entry, body):
        """Run _latest with this server's /SUMS path answering with body."""
        with patch.object(Handler, "sums", body.encode(), create=True):
            return isodl._latest({"id": "d", "name": "D", "direct": True, "page": self.base + "/SUMS", **entry})

    def latest_with_index(self, entry, listing, matches):
        """Run _latest on an index entry with this listing; _from_sums answers per checksum URL."""
        calls = []

        def lookup(checksum_url, pattern):
            calls.append(checksum_url)
            return matches.get(checksum_url)

        with patch.object(isodl, "_read", return_value=listing), patch.object(isodl, "_from_sums", side_effect=lookup):
            return isodl._latest({"id": "d", "name": "D", "direct": True, "page": "p", **entry}), calls

    def test_index_picks_the_newest_folder_numerically(self):
        """10 beats 9 numerically, where a string sort would say 9."""
        matches = {"https://x.test/10/SUMS": ("d-10.iso", "a" * 64)}
        result, calls = self.latest_with_index(
            {"index_url": "https://x.test/", "sums": "SUMS", "pattern": r"d-\d+\.iso"},
            '<a href="10/"></a><a href="9/"></a>', matches)
        self.assertEqual(result, ("https://x.test/10/d-10.iso", "d-10.iso", "a" * 64))
        self.assertEqual(calls, ["https://x.test/10/SUMS"])

    def test_index_skips_folders_without_a_matching_iso(self):
        matches = {"https://x.test/23/SUMS": None, "https://x.test/22.3/SUMS": ("d-22.3.iso", "b" * 64)}
        result, calls = self.latest_with_index(
            {"index_url": "https://x.test/", "sums": "SUMS", "pattern": r"d-[\d.]+\.iso"},
            '<a href="23/"></a><a href="22.3/"></a><a href="22.2/"></a>', matches)
        self.assertEqual(result[0], "https://x.test/22.3/d-22.3.iso")
        self.assertEqual(calls, ["https://x.test/23/SUMS", "https://x.test/22.3/SUMS"])

    def test_index_skips_a_folder_whose_checksum_file_is_not_up_yet(self):
        def lookup(checksum_url, pattern):
            if "/23/" in checksum_url:
                raise urllib.error.HTTPError(checksum_url, 404, "Not Found", None, None)
            return "d-22.iso", "e" * 64

        entry = {"id": "d", "name": "D", "direct": True, "page": "p",
                 "index_url": "https://x.test/", "sums": "SUMS", "pattern": r"d-\d+\.iso"}
        with patch.object(isodl, "_read", return_value='<a href="23/"></a><a href="22/"></a>'), \
                patch.object(isodl, "_from_sums", side_effect=lookup):
            self.assertEqual(isodl._latest(entry)[0], "https://x.test/22/d-22.iso")

    def test_index_tries_at_most_three_folders_before_giving_up(self):
        calls = []

        def lookup(checksum_url, pattern):
            calls.append(checksum_url)
            return None

        listing = '<a href="4/"></a><a href="3/"></a><a href="2/"></a><a href="1/"></a>'
        entry = {"id": "d", "name": "D", "direct": True, "page": "p",
                 "index_url": "https://x.test/", "sums": "SUMS", "pattern": r"x\.iso"}
        with patch.object(isodl, "_read", return_value=listing), \
                patch.object(isodl, "_from_sums", side_effect=lookup), \
                self.assertRaisesRegex(ValueError, "no longer lists"):
            isodl._latest(entry)
        self.assertEqual(calls, [f"https://x.test/{v}/SUMS" for v in (4, 3, 2)])

    def test_ubuntu_index_keeps_to_lts_folders(self):
        """Only even-year .04 folders count, so a newer interim release is skipped."""
        matches = {"https://x.test/26.04.1/SUMS": ("ubuntu-26.04.1-desktop-amd64.iso", "c" * 64)}
        listing = '<a href="26.04.1/"></a><a href="26.04/"></a><a href="26.10/"></a><a href="25.10/"></a>'
        result, calls = self.latest_with_index(
            {"index_url": "https://x.test/", "sums": "SUMS", "lts": True,
             "pattern": r"ubuntu-\d+\.04[.\d]*-desktop-amd64\.iso"}, listing, matches)
        self.assertEqual(result[0], "https://x.test/26.04.1/ubuntu-26.04.1-desktop-amd64.iso")
        self.assertEqual(calls, ["https://x.test/26.04.1/SUMS"])

    def test_ubuntu_index_without_any_lts_folder_raises(self):
        listing = '<a href="26.10/"></a><a href="25.10/"></a>'
        with self.assertRaisesRegex(ValueError, "no longer lists"):
            self.latest_with_index({"index_url": "https://x.test/", "sums": "SUMS", "lts": True,
                                    "pattern": r"ubuntu-\d+\.04[.\d]*-desktop-amd64\.iso"}, listing, {})

    def test_index_listing_skips_non_version_folders(self):
        matches = {"https://x.test/18/SUMS": ("d-18.iso", "d" * 64)}
        listing = '<a href="../"></a><a href="18/"></a><a href="public/"></a><a href="sha256sum.txt"></a>'
        result, calls = self.latest_with_index(
            {"index_url": "https://x.test/", "sums": "SUMS", "pattern": r"d-\d+\.iso"}, listing, matches)
        self.assertEqual(calls, ["https://x.test/18/SUMS"])
        self.assertEqual(result[0], "https://x.test/18/d-18.iso")

    def test_fedora_style_list_picks_the_newest_stable_x86_iso(self):
        good, old, beta = "a" * 64, "b" * 64, "c" * 64
        rows = [{"version": "9", "arch": "x86_64", "variant": "KDE", "link": "https://x.test/old.iso", "sha256": old},
                {"version": "10", "arch": "x86_64", "variant": "KDE", "link": "https://x.test/new.iso", "sha256": good},
                {"version": "11 Beta", "arch": "x86_64", "variant": "KDE", "link": "https://x.test/beta.iso", "sha256": beta},
                {"version": "11", "arch": "aarch64", "variant": "KDE", "link": "https://x.test/arm.iso", "sha256": beta},
                {"version": "11", "arch": "x86_64", "variant": "Labs", "link": "https://x.test/labs.iso", "sha256": beta},
                {"version": "11", "arch": "x86_64", "variant": "KDE", "link": "https://x.test/disk.raw.xz", "sha256": beta}]
        entry = {"releases_url": self.base + "/SUMS", "variant": "KDE"}
        self.assertEqual(self.latest_with_body(entry, json.dumps(rows)), ("https://x.test/new.iso", "new.iso", good))

    def test_pop_style_build_uses_its_url_and_hash(self):
        body = json.dumps({"url": "https://x.test/pop_20.iso", "sha_sum": "A" * 64})
        self.assertEqual(self.latest_with_body({"build_url": self.base + "/SUMS"}, body),
                         ("https://x.test/pop_20.iso", "pop_20.iso", "a" * 64))

    def test_page_link_gets_its_hash_from_the_file_beside_it(self):
        with patch.object(isodl, "_read", return_value='<a href="https://x.test/2/distro-2.iso">'), \
                patch.object(isodl, "_from_sums", return_value=("distro-2.iso", "d" * 64)) as lookup:
            result = isodl._latest({"name": "D", "page": "https://x.test/", "page_regex": r"https://[^\"]+/distro-\d+\.iso"})
        self.assertEqual(result, ("https://x.test/2/distro-2.iso", "distro-2.iso", "d" * 64))
        self.assertEqual(lookup.call_args.args, ("https://x.test/2/distro-2.iso.sha256", r"distro\-2\.iso"))

    def test_a_listing_without_a_usable_hash_never_downloads_unverified(self):
        for entry, body in (({"build_url": self.base + "/SUMS"}, json.dumps({"url": "https://x.test/a.iso", "sha_sum": ""})),
                            ({"build_url": self.base + "/SUMS"}, "<html>not json</html>"),
                            ({"build_url": self.base + "/SUMS"}, "[]"),
                            ({"releases_url": self.base + "/SUMS", "variant": "KDE"}, "[]"),
                            ({"page_regex": r"https://nothing-here\.iso"}, "<html></html>")):
            with self.subTest(entry=entry, body=body), self.assertRaisesRegex(ValueError, "no longer lists"):
                self.latest_with_body(entry, body)

    def test_catalog_dry_run_needs_no_network(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(isodl.system, "DRY_RUN", True), \
                patch.object(isodl, "_request", side_effect=AssertionError("network used")):
            self.assertEqual(self.run_download("arch", Path(tmp)), 0)

    def test_every_direct_entry_can_be_resolved(self):
        for entry in isodl.CATALOG:
            self.assertEqual(entry["direct"], bool({"checksum_url", "releases_url", "build_url", "page_regex", "index_url"} & set(entry)), entry["id"])
            self.assertTrue(entry["page"].startswith("https://"), entry["id"])

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

    def test_server_named_download_still_resumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "disposition.part").write_bytes(DATA[:20])
            with patch.object(isodl.system, "progress") as progress:
                self.assertEqual(self.run_download(self.base + "/disposition", folder,
                                                   sha256=hashlib.sha256(DATA).hexdigest()), 0)
            self.assertEqual(list(folder.iterdir()), [folder / "folder.iso"])
            self.assertEqual((folder / "folder.iso").read_bytes(), DATA)
            progress.assert_any_call(98, "Downloading folder.iso")

    def test_link_from_the_environment(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"TUXUTIL_DOWNLOAD_URL": self.base + "/env.iso"}):
            folder = Path(tmp)
            self.assertEqual(self.run_download(None, folder, sha256=hashlib.sha256(DATA).hexdigest()), 0)
            self.assertEqual((folder / "env.iso").read_bytes(), DATA)

    def test_invalid_link_error_does_not_quote_it(self):
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(err):
            self.assertEqual(self.run_download(self.base + "/a b.iso?sig=SECRET", Path(tmp)), 1)
        self.assertIn("Download failed", err.getvalue())
        self.assertNotIn("SECRET", err.getvalue())

    def test_link_with_a_password_is_refused_before_any_lookup(self):
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(err), patch("socket.getaddrinfo") as lookup:
            self.assertEqual(self.run_download("http://alice:hunter2@example.test/a.iso", Path(tmp)), 1)
        lookup.assert_not_called()
        self.assertNotIn("hunter2", err.getvalue())

    def test_redirects_are_checked_before_they_are_followed(self):
        real_lookup = socket.getaddrinfo
        def lookup(host, *args, **kwargs):
            self.assertEqual(host, "127.0.0.1")
            return real_lookup(host, *args, **kwargs)
        for path, message in (("/to-password", "user name or password"), ("/to-ftp", "http and https")):
            err = io.StringIO()
            with self.subTest(path), tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(err), patch("socket.getaddrinfo", lookup):
                self.assertEqual(self.run_download(self.base + path, Path(tmp)), 1)
                self.assertIn(message, err.getvalue())
                self.assertNotIn("hunter2", err.getvalue())

    def test_a_plain_redirect_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.run_download(self.base + "/moved", Path(tmp)), 0)
            self.assertEqual((Path(tmp) / "moved.iso").read_bytes(), DATA)

    def test_part_symlink_is_never_written_through(self):
        real_open = isodl._open_resuming
        def link_appears_late(url, lock):
            opened = real_open(url, lock)
            part = Path(os.readlink(f"/proc/self/fd/{lock}"))
            part.unlink()
            part.symlink_to(part.parent / "keep.txt")
            return opened
        for name, wrapper in (("already there", real_open), ("appears late", link_appears_late)):
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp, patch.object(isodl, "_open_resuming", wrapper):
                folder = Path(tmp)
                (folder / "keep.txt").write_text("keep this")
                if wrapper is real_open:
                    (folder / "a.iso.part").symlink_to(folder / "keep.txt")
                self.assertEqual(self.run_download(self.base + "/a.iso", folder), 1)
                self.assertEqual((folder / "keep.txt").read_text(), "keep this")
                self.assertFalse((folder / "a.iso").exists())

    def test_second_download_of_the_same_file_is_refused(self):
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(err):
            part = Path(tmp) / "a.iso.part"
            part.write_bytes(DATA[:10])
            with open(part, "rb") as other:
                fcntl.flock(other, fcntl.LOCK_EX)
                self.assertEqual(self.run_download(self.base + "/a.iso", Path(tmp)), 1)
            self.assertEqual(part.read_bytes(), DATA[:10])
        self.assertIn("already writing", err.getvalue())

    def test_failed_start_leaves_no_empty_part_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.run_download("http://127.0.0.1:1/a.iso", Path(tmp)), 1)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_file_created_during_the_download_is_not_replaced(self):
        real_open = isodl._open_resuming
        def file_appears(url, lock):
            opened = real_open(url, lock)
            Path(os.readlink(f"/proc/self/fd/{lock}")).with_name("a.iso").write_text("mine")
            return opened
        with tempfile.TemporaryDirectory() as tmp, patch.object(isodl, "_open_resuming", file_appears):
            self.assertEqual(self.run_download(self.base + "/a.iso", Path(tmp), output=str(Path(tmp) / "a.iso")), 1)
            self.assertEqual((Path(tmp) / "a.iso").read_text(), "mine")
            self.assertEqual((Path(tmp) / "a.iso.part").read_bytes(), DATA)

    def test_resume_without_a_checksum_warns(self):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(out):
            (Path(tmp) / "a.iso.part").write_bytes(DATA[:10])
            self.assertEqual(self.run_download(self.base + "/a.iso", Path(tmp)), 0)
            self.assertEqual((Path(tmp) / "a.iso").read_bytes(), DATA)
        self.assertIn("was resumed", out.getvalue())

    def test_no_target_at_all_is_a_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ"):
            os.environ.pop("TUXUTIL_DOWNLOAD_URL", None)
            self.assertEqual(self.run_download(None, Path(tmp)), 1)

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
