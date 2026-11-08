"""Install and remove apps from the catalog (apps.json) via the distro package manager or flatpak."""
import json
import shutil
from pathlib import Path

from . import system

CATALOG = Path(__file__).with_name("apps.json")
# One process lists every installed name. ponytail: flatpak --columns needs flatpak >= 1.8.
NATIVE_QUERY = {
    "arch": ["pacman", "-Qq"],
    "debian": ["dpkg-query", "-W", "-f", "${db:Status-Status} ${Package}\n"],
    "fedora": ["rpm", "-qa", "--qf", "%{NAME}\\n"],
    "suse": ["rpm", "-qa", "--qf", "%{NAME}\\n"],
}


def load():
    return json.loads(CATALOG.read_text())


def _names(cmd, strict=False):
    if not cmd:
        return set()
    result = system.query(cmd)
    if result.returncode != 0:
        if strict:
            raise SystemExit(f"Could not read the installed apps with {cmd[0]}, so nothing was removed.")
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def installed_names(family, strict=False):
    """strict stops on a failed query. Removal must not mistake it for "not installed"."""
    native = _names(NATIVE_QUERY.get(family), strict)
    if family == "debian":
        # dpkg also lists removed packages that left their config files behind.
        native = {line.split()[1] for line in native if line.startswith("installed ")}
    flatpaks = set()
    # No flatpak at all is a normal answer, not a failed query.
    for line in _names(["flatpak", "list", "--app", "--columns=application"], strict and shutil.which("flatpak")):
        # A header, if a future flatpak prints one, has no dot. App ids do.
        if "." in line:
            flatpaks.add(line.split()[0])
    return native, flatpaks


# IDs that ship Ubuntu's archives (or say ID_LIKE=ubuntu). Debian itself is not in this set.
UBUNTU_IDS = {
    "ubuntu", "linuxmint", "pop", "zorin", "elementary", "neon", "kubuntu", "xubuntu",
    "lubuntu", "ubuntu-mate", "tuxedo", "ubuntustudio", "ubuntu-core",
}
FLATHUB_URL = "https://dl.flathub.org/repo/flathub.flatpakrepo"


def _distro(value):
    if isinstance(value, system.Distro):
        return value
    return system.Distro(value, value, value)


def _wants_ubuntu(distro):
    """True for Ubuntu and derivatives. Debian proper stays on the debian package names."""
    if distro.id in UBUNTU_IDS:
        return True
    if distro.family != "debian" or distro.id == "debian":
        return False
    try:
        info = system.parse_os_release(Path("/etc/os-release").read_text())
    except OSError:
        return False
    if info.get("ID", "").lower() != distro.id:
        return False
    return "ubuntu" in info.get("ID_LIKE", "").lower().split()


def packages_for(app, distro):
    """Package names for this distro. An ubuntu override wins over the debian family list."""
    distro = _distro(distro)
    packages = app.get("packages") or {}
    if _wants_ubuntu(distro) and "ubuntu" in packages:
        return list(packages["ubuntu"] or [])
    return list(packages.get(distro.family) or [])


def view(app, distro, native, flatpaks):
    pkgs = packages_for(app, distro)
    flatpak = app.get("flatpak") or ""
    native_ok = bool(pkgs) and all(pkg in native for pkg in pkgs)
    flatpak_ok = bool(flatpak) and flatpak in flatpaks
    return {
        "id": app["id"],
        "name": app["name"],
        "category": app["category"],
        "description": app["description"],
        "installed": native_ok or flatpak_ok,
        "available": bool(pkgs) or bool(flatpak),
    }


def select(catalog, ids):
    found = {app["id"]: app for app in catalog}
    missing = [app_id for app_id in ids if app_id not in found]
    if missing:
        raise SystemExit(f"Unknown app(s): {', '.join(missing)}")
    return [found[app_id] for app_id in ids]


def dedupe(items):
    seen = set()
    out = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def plan(apps, distro, installed=None):
    """Native packages in one group, flatpak ids in another. Native wins when both exist.

    With installed=(native names, flatpak ids), an app that has any variant installed
    plans exactly those, so removing it removes what is really there. An app with
    nothing installed is skipped, so one stale request cannot fail the whole batch.
    """
    native, flatpaks, unavailable = [], [], []
    for app in apps:
        pkgs = packages_for(app, distro)
        if installed:
            have_native = [pkg for pkg in pkgs if pkg in installed[0]]
            have_flatpak = [app["flatpak"]] if app.get("flatpak") in installed[1] else []
            if have_native or have_flatpak:
                native.extend(have_native)
                flatpaks.extend(have_flatpak)
            # Nothing of this app is installed, so there is nothing to remove.
            continue
        if pkgs:
            native.extend(pkgs)
        elif app.get("flatpak"):
            flatpaks.append(app["flatpak"])
        else:
            unavailable.append(app["id"])
    return dedupe(native), dedupe(flatpaks), unavailable


def commands_for(native, flatpaks, family, verb):
    """Return (needs_root, argv) steps. Flatpak uses the per-user remote and needs no root."""
    table = system.INSTALL_CMD if verb == "install" else system.REMOVE_CMD
    steps = []
    if native:
        if family not in table:
            raise SystemExit(f"Don't know how to {verb} native packages on family '{family}'.")
        if family == "debian" and verb == "install":
            # A fresh system has no package lists and old ones point at removed files.
            # One broken third-party repo must not block the install, as the old lists may still do.
            steps.append((True, ["sh", "-c", "apt-get update || true"]))
        steps.append((True, [*table[family], *native]))
    if flatpaks:
        if verb == "install":
            steps.append((False, [
                "flatpak", "remote-add", "--user", "--if-not-exists", "flathub", FLATHUB_URL,
            ]))
            steps.append((False, ["flatpak", "install", "--user", "-y", "flathub", *flatpaks]))
        else:
            # No --user: `apps list` counts per-user and system-wide installs alike.
            # ponytail: flatpak errors out if the app is in both; pick by installation if that comes up.
            steps.append((False, ["flatpak", "uninstall", "-y", *flatpaks]))
    return steps


def _run_steps(steps):
    """Run install steps. More than one root step shares a single escalation."""
    root_count = sum(1 for root, _ in steps if root)
    batch_root = root_count > 1
    groups = []
    for root, cmd in steps:
        if batch_root and root and groups and groups[-1][0]:
            groups[-1][1].append(cmd)
        else:
            groups.append((root, [cmd]))
    total = len(groups)
    for index, (root, cmds) in enumerate(groups, start=1):
        system.progress(int(index * 100 / (total + 1)), " ".join(cmds[0][:3]))
        if root and batch_root:
            system.run(system.as_root_all(cmds))
        elif root:
            system.run(system.as_root(cmds[0]))
        else:
            for cmd in cmds:
                system.run(cmd)


def cmd_list(args):
    distro = system.detect()
    native, flatpaks = installed_names(distro.family)
    rows = [view(app, distro, native, flatpaks) for app in load()]
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for row in rows:
        if row["installed"]:
            state = "installed"
        elif row["available"]:
            state = "available"
        else:
            state = "unavailable"
        print(f"{row['id']}\t{row['name']}\t{row['category']}\t{state}")
    return 0


def change(args, verb):
    distro = system.detect()
    apps = select(load(), args.ids)
    installed = installed_names(distro.family, strict=True) if verb == "remove" else None
    native, flatpaks, unavailable = plan(apps, distro, installed)
    if unavailable:
        raise SystemExit(f"Not available on {distro.family}: {', '.join(unavailable)}")
    steps = commands_for(native, flatpaks, distro.family, verb)
    if not steps:
        raise SystemExit("Nothing to do.")
    system.progress(0, f"{verb} {len(apps)} app(s)")
    _run_steps(steps)
    system.progress(100, "done")
    return 0


def cmd_install(args):
    return change(args, "install")


def cmd_remove(args):
    return change(args, "remove")


def register(sub):
    p = sub.add_parser("apps", help="install and remove catalog apps")
    commands = p.add_subparsers(dest="apps_cmd", required=True)

    listed = commands.add_parser("list", help="list catalog apps")
    listed.add_argument("--json", action="store_true")
    listed.set_defaults(func=cmd_list)

    install = commands.add_parser("install", help="install apps by id")
    install.add_argument("ids", nargs="+", metavar="ID")
    install.set_defaults(func=cmd_install)

    remove = commands.add_parser("remove", help="remove apps by id")
    remove.add_argument("ids", nargs="+", metavar="ID")
    remove.set_defaults(func=cmd_remove)
