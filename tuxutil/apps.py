"""Install and remove apps from the catalog (apps.json) via the distro package manager or flatpak."""


def register(sub):
    p = sub.add_parser("apps", help="(not implemented yet)")
    p.set_defaults(func=lambda args: print("apps: not implemented yet") or 1)
