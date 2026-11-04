"""Download supported live Linux ISOs and open-source Windows download pages."""
import argparse
import hashlib
import http.client
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from email.message import Message
from pathlib import Path

from . import system

USER_AGENT = "TuxUtil (+https://github.com/SusieeTheLinuxUser/tuxutil)"
TIMEOUT = 30
CATALOG = [
    {"id": "arch", "name": "Arch Linux", "kind": "linux", "direct": True,
     "page": "https://archlinux.org/download/", "notes": "Latest x86_64 ISO; checksum verified from the official mirror.",
     "url": "https://geo.mirror.pkgbuild.com/iso/latest/archlinux-x86_64.iso",
     "checksum_url": "https://geo.mirror.pkgbuild.com/iso/latest/sha256sums.txt",
     "checksum_name": "archlinux-x86_64.iso"},
    {"id": "cachyos", "name": "CachyOS", "kind": "linux", "direct": False,
     "page": "https://cachyos.org/download/", "notes": "Choose the live ISO on the official download page."},
    {"id": "mint", "name": "Linux Mint", "kind": "linux", "direct": False,
     "page": "https://linuxmint.com/download.php", "notes": "Choose a live edition on the official download page."},
    {"id": "ubuntu", "name": "Ubuntu", "kind": "linux", "direct": False,
     "page": "https://ubuntu.com/download/desktop", "notes": "Choose the live desktop ISO on the official download page."},
    {"id": "fedora-kde", "name": "Fedora KDE Plasma Desktop", "kind": "linux", "direct": False,
     "page": "https://fedoraproject.org/kde/", "notes": "Choose the KDE live image on the official download page."},
    {"id": "pop-os", "name": "Pop!_OS", "kind": "linux", "direct": False,
     "page": "https://pop.system76.com/", "notes": "Choose the live ISO on the official download page."},
    {"id": "zorin", "name": "Zorin OS", "kind": "linux", "direct": False,
     "page": "https://zorin.com/os/download/", "notes": "Choose a live edition on the official download page."},
    {"id": "manjaro", "name": "Manjaro", "kind": "linux", "direct": False,
     "page": "https://manjaro.org/products/download/x86", "notes": "Choose a live edition on the official download page."},
    {"id": "garuda", "name": "Garuda Linux", "kind": "linux", "direct": False,
     "page": "https://garudalinux.org/downloads.html", "notes": "Choose a live edition on the official download page."},
    {"id": "windows-11", "name": "Windows 11", "kind": "windows", "direct": False,
     "page": "https://www.microsoft.com/software-download/windows11",
     "notes": "Microsoft's links are temporary; paste a link copied from that page as TARGET."},
]


def _request(url, headers=None):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise ValueError("Only http and https URLs are allowed.")
    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)
    response = urllib.request.urlopen(urllib.request.Request(url, headers=request_headers), timeout=TIMEOUT)
    if urllib.parse.urlsplit(response.geturl()).scheme.lower() not in ("http", "https"):
        response.close()
        raise ValueError("Redirected download URL must use http or https.")
    return response


def _checksum(url, filename):
    with _request(url) as response:
        for line in response:
            match = re.fullmatch(rb"([0-9a-fA-F]{64})\s+\*?(.+?)\s*", line)
            if match and match.group(2).decode("utf-8", "replace").rsplit("/", 1)[-1] == filename:
                return match.group(1).decode("ascii").lower()
    raise ValueError(f"Could not find SHA-256 for {filename} in catalog checksum file.")


def _safe_name(value):
    value = urllib.parse.unquote(str(value)).replace("\\", "/").rsplit("/", 1)[-1]
    value = "".join(c for c in value if c >= " " and c not in "\x7f")
    if value in ("", ".", ".."):
        raise ValueError("The server did not provide a usable file name.")
    return value


def _response_name(response, url):
    disposition = Message()
    disposition["content-disposition"] = response.headers.get("Content-Disposition", "")
    name = disposition.get_filename()
    if not name:
        name = urllib.parse.urlsplit(response.geturl()).path.rsplit("/", 1)[-1]
    return _safe_name(name)


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _target(value):
    for entry in CATALOG:
        if value == entry["id"]:
            if not entry["direct"]:
                raise ValueError(f"{entry['name']} has no direct download; open {entry['page']} in a browser.")
            return entry["url"], entry
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise ValueError("TARGET must be a direct catalog ID or an http(s) URL.")
    return value, None


def cmd_list(args):
    print(json.dumps([{k: item[k] for k in ("id", "name", "kind", "direct", "page", "notes")} for item in CATALOG]))
    return 0


def cmd_download(args):
    try:
        url, entry = _target(args.target)
        expected = args.sha256.lower() if args.sha256 else None
        if expected and not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("--sha256 must contain exactly 64 hexadecimal characters.")
        if entry and not expected:
            expected = _checksum(entry["checksum_url"], entry["checksum_name"])
        folder = Path(args.dir).expanduser() if args.dir else (Path(args.output).expanduser().parent if args.output else Path.home() / "Downloads")
        if args.output:
            name = _safe_name(Path(args.output).name)
            folder = Path(args.output).expanduser().parent
            final = Path(args.output).expanduser().parent / name
        else:
            name = _safe_name(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]) if not entry else _safe_name(entry["url"].rsplit("/", 1)[-1])
            final = folder / name
        if system.DRY_RUN:
            print(f"Would download {url} to {final.absolute()}")
            return 0
        folder.mkdir(parents=True, exist_ok=True)
        part = final.with_name(final.name + ".part")
        if final.exists():
            if expected and _sha256(final) == expected:
                print(f"@saved {final.absolute()}")
                return 0
            raise ValueError(f"Refusing to overwrite existing file: {final}")
        offset = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        with _request(url, headers) as response:
            if offset and response.status != 206:
                offset = 0
                mode = "wb"
            else:
                mode = "ab" if offset else "wb"
            if offset and response.status == 206:
                content_range = response.headers.get("Content-Range", "")
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", content_range)
                if not match or int(match.group(1)) != offset:
                    raise ValueError("Server returned an invalid Range response; partial file was kept.")
                total = int(match.group(3)) if match.group(3) != "*" else None
            else:
                length = response.headers.get("Content-Length")
                total = int(length) if length and length.isdigit() else None
            if total is not None:
                free = shutil.disk_usage(folder).free
                needed = max(0, total - offset)
                if free < needed:
                    raise ValueError(f"Not enough free space: need {needed} bytes, have {free}.")
            response_name = _response_name(response, url)
            if not args.output and not (entry and offset):
                final = folder / response_name
                part = final.with_name(final.name + ".part")
                if final.exists():
                    raise ValueError(f"Refusing to overwrite existing file: {final}")
            received = offset
            with open(part, mode) as stream:
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    stream.write(block)
                    stream.flush()
                    received += len(block)
                    system.progress(min(99, received * 100 // total) if total else 0, f"Downloading {final.name}")
        if total is not None and received != total:
            raise ValueError(f"Download incomplete: expected {total} bytes, received {received}; partial file kept.")
        actual = _sha256(part)
        if expected and actual != expected:
            part.unlink(missing_ok=True)
            raise ValueError(f"SHA-256 mismatch: expected {expected}, got {actual}; partial file deleted.")
        if not expected:
            print(f"SHA-256 {actual}")
            print("Could not verify: no SHA-256 was provided or available.")
        os.replace(part, final)
        system.progress(100, "Download complete")
        print(f"@saved {final.absolute()}")
        return 0
    except (ValueError, OSError, http.client.HTTPException, urllib.error.URLError) as exc:
        print(f"Download failed: {exc}", file=sys.stderr)
        return 1


def register(subparsers):
    parser = subparsers.add_parser("iso", help="download supported ISO files")
    commands = parser.add_subparsers(dest="iso_command", required=True)
    listing = commands.add_parser("list", help="list ISO download sources")
    listing.add_argument("--json", action="store_true", required=True)
    listing.set_defaults(func=cmd_list)
    download = commands.add_parser("download", help="download an ISO by ID or URL")
    download.add_argument("target", metavar="TARGET")
    output = download.add_mutually_exclusive_group()
    output.add_argument("-o", "--output", metavar="FILE")
    output.add_argument("--dir", metavar="DIR")
    download.add_argument("--sha256", metavar="HEX")
    download.set_defaults(func=cmd_download)
