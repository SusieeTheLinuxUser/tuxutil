"""Apply and undo system tweaks from the catalog (tweaks.json)."""
import json
import os
import shutil
from pathlib import Path

from . import system

CATALOG = Path(__file__).with_name("tweaks.json")


def load():
    return json.loads(CATALOG.read_text())


def select(catalog, ids):
    found = {tweak["id"]: tweak for tweak in catalog}
    missing = [tweak_id for tweak_id in ids if tweak_id not in found]
    if missing:
        raise SystemExit(f"Unknown tweak(s): {', '.join(missing)}")
    return [found[tweak_id] for tweak_id in ids]


def unit_files():
    """Unit file names systemd knows, or None when it could not be asked.

    Real systemd always prints units, so empty output also means the question
    failed and callers must treat every unit as present.
    """
    result = system.query(["systemctl", "list-unit-files", "--no-legend", "--no-pager"])
    if result.returncode != 0 or not result.stdout.strip():
        return None
    # A masked unit can't be started or enabled, so it counts as missing.
    rows = [line.split() for line in result.stdout.splitlines() if line.strip()]
    return {row[0] for row in rows if not (len(row) > 1 and row[1].startswith("masked"))}


def unavailability_reason(tweak, family, units):
    """'' when the tweak can run here, else a short why-not string."""
    if family not in tweak["families"]:
        return f"only for {'/'.join(tweak['families'])}"
    for need in tweak.get("requires", []):
        kind, _, name = need.partition(":")
        # units None means systemd could not be asked: assume the unit exists.
        if kind == "unit":
            if units is not None and name not in units:
                return f"needs {name}"
        # A desktop user's PATH often lacks the sbin folders, where tools like ufw live.
        elif kind == "cmd" and not shutil.which(name, path=f"{os.environ.get('PATH', '')}:/usr/sbin:/sbin"):
            return f"needs {name}"
    return ""


def applied(tweak):
    """True/False from the check command's exit code, or None if it cannot run."""
    result = system.query(tweak["check"], timeout=10)
    if result.returncode == 127:
        return None
    return result.returncode == 0


def view(tweak, family, units):
    reason = unavailability_reason(tweak, family, units)
    return {
        "id": tweak["id"],
        "name": tweak["name"],
        "category": tweak["category"],
        "description": tweak["description"],
        "risk": tweak["risk"],
        "applied": None if reason else applied(tweak),
        "available": not reason,
        "reason": reason,
    }


def cmd_list(args):
    distro = system.detect()
    catalog = load()
    units = unit_files()  # one query for the whole listing, not one per tweak
    rows = [view(tweak, distro.family, units) for tweak in catalog]
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for row in rows:
        if not row["available"]:
            state = f"unavailable ({row['reason']})"
        elif row["applied"] is None:
            state = "unknown"
        elif row["applied"]:
            state = "applied"
        else:
            state = "not applied"
        print(f"{row['id']}\t{row['name']}\t{row['risk']}\t{state}")
    return 0


def run_actions(args, key):
    distro = system.detect()
    chosen = select(load(), args.ids)
    units = unit_files()
    reasons = {tweak["id"]: unavailability_reason(tweak, distro.family, units) for tweak in chosen}
    missing = [f"{tweak_id} ({reason})" for tweak_id, reason in reasons.items() if reason]
    if missing:
        raise SystemExit(f"Not available on {distro.family}: {', '.join(missing)}")

    runnable = []
    for tweak in chosen:
        state = applied(tweak)
        if key == "apply" and state is True:
            print(f"skipping {tweak['id']}: already applied")
            continue
        if key == "undo" and state is False:
            print(f"skipping {tweak['id']}: not applied")
            continue
        # None means the check could not run, so proceed either way.
        runnable.append(tweak)
    if not runnable:
        system.progress(100, "nothing to do")
        return 0

    steps = []
    for tweak in runnable:
        root = bool(tweak.get("root", True))
        for cmd in tweak[key]:
            steps.append((tweak["id"], cmd, root))
    groups = []
    for tweak_id, cmd, root in steps:
        if groups and groups[-1][0] == root:
            groups[-1][1].append((tweak_id, cmd))
        else:
            groups.append((root, [(tweak_id, cmd)]))

    system.progress(0, f"{key} {len(runnable)} tweak(s)")
    total = len(groups)
    for index, (root, items) in enumerate(groups, start=1):
        label = ",".join(dict.fromkeys(tweak_id for tweak_id, _ in items))
        system.progress(int(index * 100 / (total + 1)), label)
        cmds = [cmd for _, cmd in items]
        if root:
            system.run(system.as_root_all(cmds))
        else:
            for cmd in cmds:
                system.run(cmd)
    system.progress(100, "done")
    return 0


def cmd_apply(args):
    return run_actions(args, "apply")


def cmd_undo(args):
    return run_actions(args, "undo")


def register(sub):
    p = sub.add_parser("tweaks", help="apply and undo system tweaks")
    commands = p.add_subparsers(dest="tweaks_cmd", required=True)

    listed = commands.add_parser("list", help="list tweaks")
    listed.add_argument("--json", action="store_true")
    listed.set_defaults(func=cmd_list)

    apply = commands.add_parser("apply", help="apply tweaks by id")
    apply.add_argument("ids", nargs="+", metavar="ID")
    apply.set_defaults(func=cmd_apply)

    undo = commands.add_parser("undo", help="undo tweaks by id")
    undo.add_argument("ids", nargs="+", metavar="ID")
    undo.set_defaults(func=cmd_undo)
