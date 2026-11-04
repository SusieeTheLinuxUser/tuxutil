"""Build a custom Windows 11 ISO (the MicroWin equivalent), rootless."""


def register(sub):
    p = sub.add_parser("winiso", help="(not implemented yet)")
    p.set_defaults(func=lambda args: print("winiso: not implemented yet") or 1)
