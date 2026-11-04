"""Distro detection, command running, privilege escalation, external tool checks."""
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Global --dry-run: run() prints commands instead of executing them.
DRY_RUN = False

# family -> command prefix that installs packages non-interactively.
INSTALL_CMD = {
    "arch": ["pacman", "-S", "--needed", "--noconfirm"],
    "debian": ["apt-get", "install", "-y"],
    "fedora": ["dnf", "install", "-y"],
    "suse": ["zypper", "-n", "install"],
    "void": ["xbps-install", "-y"],
    "alpine": ["apk", "add"],
    "solus": ["eopkg", "install", "-y"],
    "gentoo": ["emerge", "--noreplace"],
}
REMOVE_CMD = {
    "arch": ["pacman", "-Rns", "--noconfirm"],
    "debian": ["apt-get", "remove", "-y"],
    "fedora": ["dnf", "remove", "-y"],
    "suse": ["zypper", "-n", "remove"],
    "void": ["xbps-remove", "-y"],
    "alpine": ["apk", "del"],
    "solus": ["eopkg", "remove", "-y"],
    "gentoo": ["emerge", "--depclean"],
}
UPDATE_CMDS = {
    "arch": [["pacman", "-Syu", "--noconfirm"]],
    "debian": [["apt-get", "update"], ["apt-get", "dist-upgrade", "-y"]],
    "fedora": [["dnf", "upgrade", "-y"]],
    "suse": [["zypper", "-n", "dup"]],
    "void": [["xbps-install", "-Syu"]],
    "alpine": [["apk", "upgrade", "-U"]],
    "solus": [["eopkg", "upgrade", "-y"]],
    "gentoo": [["emerge", "--sync"], ["emerge", "-uDN", "@world"]],
}
# ID / ID_LIKE tokens -> family. Anything derived names its parent in ID_LIKE.
FAMILY_IDS = {
    "arch": "arch", "archlinux": "arch", "cachyos": "arch", "manjaro": "arch", "endeavouros": "arch", "garuda": "arch",
    "debian": "debian", "ubuntu": "debian", "linuxmint": "debian", "pop": "debian", "zorin": "debian", "kali": "debian",
    "fedora": "fedora", "rhel": "fedora", "centos": "fedora", "rocky": "fedora", "almalinux": "fedora", "nobara": "fedora",
    "suse": "suse", "opensuse": "suse", "opensuse-tumbleweed": "suse", "opensuse-leap": "suse", "sles": "suse",
    "void": "void", "alpine": "alpine", "solus": "solus", "gentoo": "gentoo", "nixos": "nixos",
}
# Probed when os-release is missing or names nothing we know.
PKG_BINARIES = {"pacman": "arch", "apt-get": "debian", "dnf": "fedora", "zypper": "suse",
                "xbps-install": "void", "apk": "alpine", "eopkg": "solus", "emerge": "gentoo"}
# External tool -> package per family (missing family = same name as the tool).
TOOL_PACKAGES = {
    "xorriso": {},
    "wimlib-imagex": {"arch": "wimlib", "debian": "wimtools", "fedora": "wimlib-utils", "suse": "wimtools", "alpine": "wimlib", "void": "wimlib"},
    "mksquashfs": {"default": "squashfs-tools"},
    "unsquashfs": {"default": "squashfs-tools"},
    "sfdisk": {"default": "util-linux", "debian": "fdisk"},
    "mkfs.vfat": {"default": "dosfstools"},
    "mcopy": {"default": "mtools"},
    "lsblk": {"default": "util-linux"},
    "unshare": {"default": "util-linux"},
    "flatpak": {},
    "qemu-system-x86_64": {"arch": "qemu-full", "debian": "qemu-system-x86", "fedora": "qemu-system-x86", "suse": "qemu-x86"},
    # 7z reads UDF. xorriso only sees the ISO9660 bridge on Microsoft ISOs.
    "7z": {"arch": "7zip", "debian": "p7zip-full", "fedora": "p7zip-plugins", "suse": "7zip", "alpine": "7zip", "void": "7zip"},
}


@dataclass(frozen=True)
class Distro:
    id: str
    name: str
    family: str  # key of INSTALL_CMD, "nixos", or "unknown"


def parse_os_release(text):
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            parts = shlex.split(val)
            out[key] = parts[0] if parts else ""
    return out


def detect(os_release="/etc/os-release"):
    """Read os-release, map ID then ID_LIKE to a family, then probe package managers."""
    try:
        info = parse_os_release(Path(os_release).read_text())
    except OSError:
        info = {}
    ident = info.get("ID", "unknown").lower()
    for token in [ident, *info.get("ID_LIKE", "").lower().split()]:
        if token in FAMILY_IDS:
            return Distro(ident, info.get("PRETTY_NAME", ident), FAMILY_IDS[token])
    for binary, family in PKG_BINARIES.items():
        if shutil.which(binary):
            return Distro(ident, info.get("PRETTY_NAME", ident), family)
    return Distro(ident, info.get("PRETTY_NAME", ident), "unknown")


def tool_package(tool, family):
    spec = TOOL_PACKAGES.get(tool, {})
    return spec.get(family) or spec.get("default") or tool


def missing_tools(tools):
    return [t for t in tools if not shutil.which(t)]


def require_tools(tools):
    """Exit with an actionable message when external tools are absent."""
    missing = missing_tools(tools)
    if missing:
        family = detect().family
        pkgs = sorted({tool_package(t, family) for t in missing})
        raise SystemExit(f"Missing tools: {', '.join(missing)}. Install packages: {' '.join(pkgs)} "
                         f"(or run: tuxutil doctor --install)")


def as_root(cmd):
    """Prefix cmd so it runs as root: nothing if root, pkexec on a desktop, else sudo."""
    if os.geteuid() == 0:
        return list(cmd)
    desktop = os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    if desktop and shutil.which("pkexec"):
        return ["pkexec", *cmd]
    if shutil.which("sudo"):
        return ["sudo", *cmd]
    if shutil.which("doas"):
        return ["doas", *cmd]
    if DRY_RUN:
        return list(cmd)
    raise SystemExit("Need root but found no pkexec, sudo or doas.")


def as_root_all(cmds):
    """One escalation for every command in cmds, joined with &&."""
    script = " && ".join(shlex.join(map(str, cmd)) for cmd in cmds)
    return as_root(["sh", "-c", script])


def run(cmd, *, check=True, capture=False, **kwargs):
    """Run an external command, echoing it. Honors DRY_RUN. Never uses a shell."""
    # Captured commands feed --json listings, so their echo must stay off stdout.
    print("$ " + shlex.join(map(str, cmd)), flush=True, file=sys.stderr if capture else sys.stdout)
    if DRY_RUN:
        return subprocess.CompletedProcess(cmd, 0, "", "")
    if capture:
        kwargs.update(stdout=subprocess.PIPE, text=True)
    return subprocess.run([str(c) for c in cmd], check=check, **kwargs)


def query(cmd, timeout=15):
    """Run a read-only command and return its result. Never dry-runs and never raises."""
    try:
        return subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                              check=False, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return subprocess.CompletedProcess(list(cmd), 127, "", "")


def progress(percent, message=""):
    """Machine-readable progress line, parsed by the GUI."""
    print(f"@progress {int(percent)} {message}", flush=True)


def self_cmd():
    """Argv prefix that re-invokes tuxutil, e.g. under pkexec."""
    return [sys.executable, "-m", "tuxutil"]
