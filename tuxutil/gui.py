"""PySide6 GUI. A frontend over the CLI: it runs `tuxutil ...` subprocesses and reads their output."""


def register(sub):
    p = sub.add_parser("gui", help="(not implemented yet)")
    p.set_defaults(func=lambda args: print("gui: not implemented yet") or 1)
