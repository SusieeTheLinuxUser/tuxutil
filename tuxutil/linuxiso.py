"""Remaster a Linux live ISO into a custom one, rootless."""


def register(sub):
    p = sub.add_parser("linuxiso", help="(not implemented yet)")
    p.set_defaults(func=lambda args: print("linuxiso: not implemented yet") or 1)
