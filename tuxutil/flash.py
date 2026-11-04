"""Write an ISO to a USB stick, safely."""


def register(sub):
    p = sub.add_parser("flash", help="(not implemented yet)")
    p.set_defaults(func=lambda args: print("flash: not implemented yet") or 1)
