"""Apply and undo system tweaks from the catalog (tweaks.json)."""
import json
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


def available(tweak, family):
    return family in tweak["families"]


def applied(tweak):
    """True/False from the check command's exit code, or None if it cannot run."""
    result = system.query(tweak["check"], timeout=10)
    if result.returncode == 127:
        return None
    return result.returncode == 0


def view(tweak, family):
    ok = available(tweak, family)
    return {
        "id": tweak["id"],
        "name": tweak["name"],
        "category": tweak["category"],
        "description": tweak["description"],
        "risk": tweak["risk"],
        "applied": applied(tweak) if ok else None,
        "available": ok,
    }


def cmd_list(args):
    distro = system.detect()
    rows = [view(tweak, distro.family) for tweak in load()]
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for row in rows:
        if not row["available"]:
            state = "unavailable"
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
    missing = [tweak["id"] for tweak in chosen if not available(tweak, distro.family)]
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
