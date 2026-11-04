"""Install and remove apps from the catalog (apps.json) via the distro package manager or flatpak."""
import json
from pathlib import Path

from . import system

CATALOG = Path(__file__).with_name("apps.json")
# One process lists every installed name. ponytail: flatpak --columns needs flatpak >= 1.8.
NATIVE_QUERY = {
    "arch": ["pacman", "-Qq"],
    "debian": ["dpkg-query", "-W", "-f", "${Package}\n"],
    "fedora": ["rpm", "-qa", "--qf", "%{NAME}\\n"],
    "suse": ["rpm", "-qa", "--qf", "%{NAME}\\n"],
}


def load():
    return json.loads(CATALOG.read_text())


def _names(cmd):
    if not cmd:
        return set()
    result = system.query(cmd)
    if result.returncode != 0:
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def installed_names(family):
    native = _names(NATIVE_QUERY.get(family))
    flatpaks = set()
    for line in _names(["flatpak", "list", "--app", "--columns=application"]):
        # A header, if a future flatpak prints one, has no dot. App ids do.
        if "." in line:
            flatpaks.add(line.split()[0])
    return native, flatpaks


def packages_for(app, family):
    return list(app.get("packages", {}).get(family) or [])


def view(app, family, native, flatpaks):
    pkgs = packages_for(app, family)
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


def plan(apps, family):
    """Native packages in one group, flatpak ids in another. Native wins when both exist."""
    native, flatpaks, unavailable = [], [], []
    for app in apps:
        pkgs = packages_for(app, family)
        if pkgs:
            native.extend(pkgs)
        elif app.get("flatpak"):
            flatpaks.append(app["flatpak"])
        else:
            unavailable.append(app["id"])
    return dedupe(native), dedupe(flatpaks), unavailable


def commands_for(native, flatpaks, family, verb):
    table = system.INSTALL_CMD if verb == "install" else system.REMOVE_CMD
    cmds = []
    if native:
        if family not in table:
            raise SystemExit(f"Don't know how to {verb} native packages on family '{family}'.")
        cmds.append([*table[family], *native])
    if flatpaks:
        if verb == "install":
            cmds.append(["flatpak", "install", "-y", "flathub", *flatpaks])
        else:
            cmds.append(["flatpak", "uninstall", "-y", *flatpaks])
    return cmds


def cmd_list(args):
    distro = system.detect()
    native, flatpaks = installed_names(distro.family)
    rows = [view(app, distro.family, native, flatpaks) for app in load()]
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
    native, flatpaks, unavailable = plan(apps, distro.family)
    if unavailable:
        raise SystemExit(f"Not available on {distro.family}: {', '.join(unavailable)}")
    cmds = commands_for(native, flatpaks, distro.family, verb)
    if not cmds:
        raise SystemExit("Nothing to do.")
    system.progress(0, f"{verb} {len(apps)} app(s)")
    for index, cmd in enumerate(cmds, start=1):
        system.progress(int(index * 100 / (len(cmds) + 1)), " ".join(cmd[:3]))
        system.run(system.as_root(cmd))
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
