"""Apply and undo system tweaks from the catalog (tweaks.json)."""


def register(sub):
    p = sub.add_parser("tweaks", help="(not implemented yet)")
    p.set_defaults(func=lambda args: print("tweaks: not implemented yet") or 1)
