"""Check GitHub for a newer tuxutil release. The releases/latest page redirects
to .../releases/tag/vX.Y.Z, so no API (60 req/hour) is spent. Nothing is downloaded."""
import re
import urllib.error
import urllib.request
from importlib import metadata
from pathlib import Path

from . import __version__

RELEASES_URL = "https://github.com/SusieeTheLinuxUser/tuxutil/releases/latest"


def current_version():
    # run.sh runs straight from a source tarball: its pyproject.toml is the truth, and an older
    # installed copy (metadata) or the hand-kept __version__ would be wrong.
    try:
        m = re.search(r'^version = "([^"]+)"', (Path(__file__).parent.parent / "pyproject.toml").read_text(), re.M)
        if m:
            return m.group(1)
    except OSError:
        pass
    try:
        return metadata.version("tuxutil")
    except metadata.PackageNotFoundError:
        return __version__


def parse_version(text):
    """'v0.2.10' or '0.2.10' -> (0, 2, 10); anything else -> None."""
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)", str(text).strip())
    return tuple(int(p) for p in m.group(1).split(".")) if m else None


def version_from_location(location):
    """The version in a .../releases/tag/vX.Y.Z redirect target, or None."""
    if not location:
        return None
    return parse_version(str(location).rstrip("/").rsplit("/", 1)[-1])


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Stops at the 302, so its Location header can be read."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def latest_version():
    """Latest release version tuple, or None when the check fails. Never raises."""
    try:
        resp = urllib.request.build_opener(NoRedirect).open(  # the 302 itself names the tag
            urllib.request.Request(RELEASES_URL, method="HEAD"), timeout=10)
    except urllib.error.HTTPError as e:
        resp = e
    except Exception:
        return None
    try:
        return version_from_location(resp.headers.get("Location", ""))
    finally:
        resp.close()


def status():
    """One dict about tuxutil itself, for doctor's JSON and text line."""
    cur, latest = current_version(), latest_version()
    cur_t = parse_version(cur)  # None for e.g. a "0.2.3.dev0" build: then no update is claimed
    return {
        "version": cur,
        "latest": ".".join(map(str, latest)) if latest else None,
        "update_available": bool(latest and cur_t and latest > cur_t),
        "update_url": RELEASES_URL,
    }


def describe(st):
    """The one doctor line: up to date, out of date, or couldn't check."""
    if st["latest"] is None:
        return f"tuxutil {st['version']} (couldn't check for updates)"
    if st["update_available"]:
        return f"tuxutil {st['version']}, {st['latest']} is out: {st['update_url']}"
    return f"tuxutil {st['version']} (latest)"