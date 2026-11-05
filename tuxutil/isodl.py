"""Download supported live Linux ISOs and open-source Windows download pages."""
import argparse
import fcntl
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
MAX_LISTING = 5_000_000
CATALOG = [
    {"id": "arch", "name": "Arch Linux", "kind": "linux", "direct": True,
     "page": "https://archlinux.org/download/", "notes": "Latest x86_64 ISO; checksum verified from the official mirror.",
     "url": "https://geo.mirror.pkgbuild.com/iso/latest/archlinux-x86_64.iso",
     "checksum_url": "https://geo.mirror.pkgbuild.com/iso/latest/sha256sums.txt",
     "pattern": r"archlinux-x86_64\.iso"},
    {"id": "cachyos", "name": "CachyOS Desktop", "kind": "linux", "direct": True,
     "page": "https://cachyos.org/download/", "notes": "Newest build named on CachyOS's download page; checksum verified from its own file.",
     "page_regex": r"https://iso\.cachyos\.org/desktop/\d+/cachyos-desktop-linux-\d+\.iso"},
    {"id": "mint", "name": "Linux Mint Cinnamon", "kind": "linux", "direct": True,
     "page": "https://linuxmint.com/download.php", "notes": "Newest release from the official mirror; checksum verified from its own list.",
     "index_url": "https://mirrors.edge.kernel.org/linuxmint/stable/",
     "sums": "sha256sum.txt",
     "pattern": r"linuxmint-\d+(\.\d+)?-cinnamon-64bit\.iso"},
    {"id": "ubuntu", "name": "Ubuntu LTS Desktop", "kind": "linux", "direct": True,
     "page": "https://ubuntu.com/download/desktop", "notes": "Newest LTS release; checksum verified from Ubuntu's own list.",
     "index_url": "https://releases.ubuntu.com/", "lts": True,
     "sums": "SHA256SUMS",
     "pattern": r"ubuntu-\d+\.04[.\d]*-desktop-amd64\.iso"},
    {"id": "fedora-kde", "name": "Fedora KDE Plasma Desktop", "kind": "linux", "direct": True,
     "page": "https://fedoraproject.org/kde/", "notes": "Newest stable release; checksum verified from Fedora's release list.",
     "releases_url": "https://fedoraproject.org/releases.json", "variant": "KDE"},
    {"id": "pop-os", "name": "Pop!_OS 24.04 (Intel/AMD)", "kind": "linux", "direct": True,
     "page": "https://system76.com/pop/download/", "notes": "Newest build; checksum verified from System76's build list.",
     "build_url": "https://api.pop-os.org/builds/24.04/intel"},
    # ponytail: Pop's version stays hard-coded (its API answers only known versions and has no listing); update it on a new release.
    {"id": "pop-os-nvidia", "name": "Pop!_OS 24.04 (NVIDIA)", "kind": "linux", "direct": True,
     "page": "https://system76.com/pop/download/", "notes": "Newest build; checksum verified from System76's build list.",
     "build_url": "https://api.pop-os.org/builds/24.04/nvidia"},
    {"id": "zorin", "name": "Zorin OS Core", "kind": "linux", "direct": True,
     "page": "https://zorin.com/os/download/", "notes": "Newest release from the official mirror's list.",
     "index_url": "https://mirrors.edge.kernel.org/zorinos-isos/",
     "sums": "SHA256SUMS.txt",
     "pattern": r"Zorin-OS-\d+(\.\d+)?-Core-64-bit(-r\d+)?\.iso"},
    {"id": "manjaro", "name": "Manjaro KDE Plasma", "kind": "linux", "direct": True,
     "page": "https://manjaro.org/products/download/x86", "notes": "Newest build named on Manjaro's download page; checksum verified from its own file.",
     "page_regex": r"https://download\.manjaro\.org/kde/[\d.]+/manjaro-kde-[\w.-]+\.iso"},
    {"id": "garuda", "name": "Garuda Linux Dr460nized", "kind": "linux", "direct": True,
     "page": "https://garudalinux.org/downloads.html", "notes": "Newest build; checksum verified from Garuda's own file.",
     "url": "https://iso.builds.garudalinux.org/iso/latest/garuda/dr460nized/latest.iso",
     "checksum_url": "https://iso.builds.garudalinux.org/iso/latest/garuda/dr460nized/latest.iso.sha256",
     "pattern": r"garuda-[\w.-]+\.iso"},
    {"id": "windows-11", "name": "Windows 11", "kind": "windows", "direct": False,
     "page": "https://www.microsoft.com/software-download/windows11",
     "notes": "Pick your language on Microsoft's page and press its download button. TuxUtil takes the file from there. "
              "If the page opens in your browser instead, copy its download link and paste it here."},
]


def _check_url(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise ValueError("Only http and https URLs are allowed.")
    if "@" in parsed.netloc:
        # urllib would look up "user:password@host" as a host name, sending the password to DNS.
        raise ValueError("Links with a user name or password are not supported.")


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_CheckedRedirects)


def _request(url, headers=None):
    _check_url(url)
    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)
    return _opener.open(urllib.request.Request(url, headers=request_headers), timeout=TIMEOUT)


def _read(url):
    with _request(url) as response:
        return response.read(MAX_LISTING).decode("utf-8", "replace")


def _from_sums(checksum_url, pattern):
    """Return (file name, SHA-256) of the last file a checksum list names that fits the pattern, or None."""
    found = None
    with _request(checksum_url) as response:
        for line in response:
            match = re.fullmatch(rb"([0-9a-fA-F]{64})\s+\*?(.+?)\s*", line)
            if match:
                name = match.group(2).decode("utf-8", "replace").rsplit("/", 1)[-1]
                if re.fullmatch(pattern, name):
                    found = name, match.group(1).decode("ascii").lower()
    return found


def _index_folders(entry):
    """The entry's version folders from its directory listing, newest first, at most three."""
    folders = []
    for name in re.findall(r'href="([^"?/]+)/"', _read(entry["index_url"])):
        parts = name.split(".")
        if not all(part.isdigit() for part in parts):
            continue
        if entry.get("lts") and (int(parts[0]) % 2 or parts[1:2] != ["04"]):
            continue
        folders.append((tuple(int(part) for part in parts), name))
    folders.sort(reverse=True)
    return [name for _, name in folders[:3]]


def _latest(entry):
    """Return (url, file name, SHA-256) of the entry's newest ISO, as its official site lists it."""
    url = entry.get("url")
    found = None
    try:
        if "releases_url" in entry:
            rows = [row for row in json.loads(_read(entry["releases_url"]))
                    if row.get("arch") == "x86_64" and row.get("variant") == entry["variant"]
                    and row.get("version", "").isdigit() and row.get("link", "").endswith(".iso")]
            if rows:
                newest = max(rows, key=lambda row: int(row["version"]))
                url = newest["link"]
                found = url.rsplit("/", 1)[-1], newest["sha256"].lower()
        elif "build_url" in entry:
            build = json.loads(_read(entry["build_url"]))
            url = build["url"]
            found = url.rsplit("/", 1)[-1], build["sha_sum"].lower()
        elif "page_regex" in entry:
            match = re.search(entry["page_regex"], _read(entry["page"]))
            if match:
                url = match.group(0)
                found = _from_sums(url + ".sha256", re.escape(url.rsplit("/", 1)[-1]))
        elif "index_url" in entry:
            # A newest folder can exist before its ISO does, so an empty list falls through to older ones.
            for folder in _index_folders(entry):
                found = _from_sums(f"{entry['index_url']}{folder}/{entry['sums']}", entry["pattern"])
                if found:
                    url = f"{entry['index_url']}{folder}/{found[0]}"
                    break
        else:
            found = _from_sums(entry["checksum_url"], entry["pattern"])
    except (AttributeError, KeyError, TypeError, json.JSONDecodeError):
        found = None
    # An empty hash here would turn a catalog download into an unverified one.
    if not found or not re.fullmatch(r"[0-9a-f]{64}", found[1]):
        raise ValueError(f"{entry['name']}'s official site no longer lists a matching ISO; open {entry['page']} in a browser.")
    return url or entry["checksum_url"].rsplit("/", 1)[0] + "/" + found[0], found[0], found[1]


def _open_resuming(url, lock):
    """Open url, resuming the locked .part if possible. Returns (response, offset); no response means it's already whole."""
    offset = os.fstat(lock).st_size
    try:
        return _request(url, {"Range": f"bytes={offset}-"} if offset else None), offset
    except urllib.error.HTTPError as exc:
        if exc.code != 416 or not offset:
            raise
        # Nothing left past the end: killed while checking the finished file, or the file on the server changed.
        if (exc.headers.get("Content-Range") or "").endswith(f"/{offset}"):
            return None, offset
        os.ftruncate(lock, 0)
        return _request(url), 0


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
            return None, entry
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise ValueError("TARGET must be a direct catalog ID or an http(s) URL.")
    return value, None


def cmd_list(args):
    print(json.dumps([{k: item[k] for k in ("id", "name", "kind", "direct", "page", "notes")} for item in CATALOG]))
    return 0


def _lock_part(part):
    """Hold the .part file for this run, so two downloads can't write into it at once."""
    if part.is_symlink():
        raise ValueError(f"Refusing to write through a symbolic link: {part}")
    lock = os.open(part, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o666)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock)
        raise ValueError(f"Another download is already writing {part}.") from None
    return lock


def cmd_download(args):
    lock = None
    part = None
    try:
        # The GUI sends pasted links here, because a signed link in argv is readable by other local users.
        target = args.target or os.environ.pop("TUXUTIL_DOWNLOAD_URL", "")
        if not target:
            raise ValueError("TARGET is required: a catalog ID or an http(s) URL.")
        url, entry = _target(target)
        expected = args.sha256.lower() if args.sha256 else None
        if expected and not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("--sha256 must contain exactly 64 hexadecimal characters.")
        output = Path(args.output).expanduser() if args.output else None
        folder = output.parent if output else (Path(args.dir).expanduser() if args.dir else Path.home() / "Downloads")
        if system.DRY_RUN:
            print(f"Would download {entry['id'] if entry else urllib.parse.urlsplit(url).hostname} to {(output or folder).absolute()}")
            return 0
        name = None
        if entry:
            url, name, listed = _latest(entry)
            expected = expected or listed
        final = output or folder / _safe_name(name or urllib.parse.urlsplit(url).path.rstrip("/") or "download.iso")
        folder.mkdir(parents=True, exist_ok=True)
        part = final.with_name(final.name + ".part")
        if final.exists():
            if expected and _sha256(final) == expected:
                print(f"@saved {final.absolute()}")
                return 0
            raise ValueError(f"Refusing to overwrite existing file: {final}")
        # ponytail: a pasted link whose file changed, or another link with the same name, resumes into a mixed file.
        # Only a checksum catches that, so a resume without one warns below. Needs an ETag check.
        lock = _lock_part(part)
        response, offset = _open_resuming(url, lock)
        total = offset
        if response:
            with response:
                if offset and response.status != 206:
                    offset = 0
                if offset:
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
                if not output and not entry:
                    # The server's name beats the link's. Catalog files keep the name their checksum list uses.
                    # The .part keeps the link's name, because that's what the next run looks for to resume.
                    final = folder / _response_name(response, url)
                    if final.exists():
                        raise ValueError(f"Refusing to overwrite existing file: {final}")
                received = offset
                shown = None
                # O_NOFOLLOW: a link swapped in since the check above must not redirect the write.
                flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | (os.O_APPEND if offset else os.O_TRUNC)
                with os.fdopen(os.open(part, flags, 0o666), "ab" if offset else "wb") as stream:
                    while True:
                        block = response.read(1024 * 1024)
                        if not block:
                            break
                        stream.write(block)
                        stream.flush()
                        received += len(block)
                        percent = min(98, received * 100 // total) if total else 0
                        if percent != shown:
                            system.progress(percent, f"Downloading {final.name}")
                            shown = percent
            if total is not None and received != total:
                raise ValueError(f"Download incomplete: expected {total} bytes, received {received}; partial file kept.")
        system.progress(99, "Checking the download")
        actual = _sha256(part)
        if expected and actual != expected:
            part.unlink(missing_ok=True)
            raise ValueError(f"SHA-256 mismatch: expected {expected}, got {actual}; partial file deleted.")
        if not expected:
            print(f"SHA-256 {actual}")
            print("Could not verify: no SHA-256 was provided or available.")
            if offset:
                print("Warning: this download was resumed. If the file changed on the server, the result is corrupt.")
        # ponytail: a file created between this check and the rename is still replaced; os.link fails on FAT and exFAT.
        if final.exists():
            raise ValueError(f"Refusing to overwrite existing file: {final}; partial file kept.")
        os.replace(part, final)
        system.progress(100, "Download complete")
        print(f"@saved {final.absolute()}")
        return 0
    except http.client.InvalidURL:
        # Its text quotes the link, and a signed link's query string is its password.
        print("Download failed: the link is not a valid URL. Spaces must be written as %20.", file=sys.stderr)
        return 1
    except (ValueError, OSError, http.client.HTTPException, urllib.error.URLError) as exc:
        print(f"Download failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if lock is not None:
            if not os.fstat(lock).st_size:
                part.unlink(missing_ok=True)  # Don't leave an empty file behind when the download never started.
            os.close(lock)


def register(subparsers):
    parser = subparsers.add_parser("iso", help="download supported ISO files")
    commands = parser.add_subparsers(dest="iso_command", required=True)
    listing = commands.add_parser("list", help="list ISO download sources")
    listing.add_argument("--json", action="store_true", required=True)
    listing.set_defaults(func=cmd_list)
    download = commands.add_parser("download", help="download an ISO by ID or URL")
    download.add_argument("target", metavar="TARGET", nargs="?", help="catalog ID or http(s) URL; default: $TUXUTIL_DOWNLOAD_URL")
    output = download.add_mutually_exclusive_group()
    output.add_argument("-o", "--output", metavar="FILE")
    output.add_argument("--dir", metavar="DIR")
    download.add_argument("--sha256", metavar="HEX")
    download.set_defaults(func=cmd_download)
