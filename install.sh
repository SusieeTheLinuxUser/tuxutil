#!/bin/sh
# Install TuxUtil for the current user.
# The distro package manager runs as root only when python3 itself is missing.
set -eu

REPO_URL="https://github.com/SusieeTheLinuxUser/tuxutil.git"
LOCAL=""

usage() {
    printf 'Usage: %s [--from-local DIR]\n' "$0" >&2
    exit 2
}

while [ $# -gt 0 ]; do
    case $1 in
        --from-local)
            [ $# -ge 2 ] || usage
            LOCAL=$2
            shift 2
            ;;
        -h|--help)
            usage
            ;;
        *)
            usage
            ;;
    esac
done

if [ -n "$LOCAL" ] && [ ! -f "$LOCAL/pyproject.toml" ]; then
    printf 'No pyproject.toml in %s\n' "$LOCAL" >&2
    exit 1
fi

PREFIX="${HOME}/.local"
SHARE="${PREFIX}/share/tuxutil"
VENV="${SHARE}/venv"
BINDIR="${PREFIX}/bin"
APPDIR="${PREFIX}/share/applications"
DESKTOP="${APPDIR}/tuxutil.desktop"
LAUNCHER="${BINDIR}/tuxutil"

case $VENV in
    *\"*|*"'"*|*\\*)
        printf 'Refusing to install: the path contains a quote: %s\n' "$VENV" >&2
        exit 1
        ;;
esac

family() {
    os_id=""
    os_like=""
    if [ -r /etc/os-release ]; then
        while IFS= read -r line || [ -n "$line" ]; do
            case $line in
                ID=*)
                    os_id=${line#ID=}
                    os_id=${os_id#\"}
                    os_id=${os_id%\"}
                    ;;
                ID_LIKE=*)
                    os_like=${line#ID_LIKE=}
                    os_like=${os_like#\"}
                    os_like=${os_like%\"}
                    ;;
            esac
        done < /etc/os-release
    fi
    set -f
    for token in $(printf '%s %s' "$os_id" "$os_like" | tr '[:upper:]' '[:lower:]'); do
        case $token in
            arch|archlinux|cachyos|manjaro|endeavouros|garuda)
                set +f
                printf '%s\n' arch
                return
                ;;
            debian|ubuntu|linuxmint|pop|zorin|kali)
                set +f
                printf '%s\n' debian
                return
                ;;
            fedora|rhel|centos|rocky|almalinux|nobara)
                set +f
                printf '%s\n' fedora
                return
                ;;
            suse|opensuse|opensuse-tumbleweed|opensuse-leap|sles)
                set +f
                printf '%s\n' suse
                return
                ;;
            void)
                set +f
                printf '%s\n' void
                return
                ;;
            alpine)
                set +f
                printf '%s\n' alpine
                return
                ;;
        esac
    done
    set +f
    if command -v pacman >/dev/null 2>&1; then
        printf '%s\n' arch
        return
    fi
    if command -v apt-get >/dev/null 2>&1; then
        printf '%s\n' debian
        return
    fi
    if command -v dnf >/dev/null 2>&1; then
        printf '%s\n' fedora
        return
    fi
    if command -v zypper >/dev/null 2>&1; then
        printf '%s\n' suse
        return
    fi
    printf '%s\n' unknown
}

as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    elif command -v doas >/dev/null 2>&1; then
        doas "$@"
    else
        printf 'Need root to install Python, and no sudo or doas was found.\n' >&2
        exit 1
    fi
}

ensure_python() {
    if command -v python3 >/dev/null 2>&1; then
        return 0
    fi
    printf 'python3 is missing; installing it with the distro package manager.\n'
    fam=$(family)
    case $fam in
        arch)
            as_root pacman -S --needed --noconfirm python
            ;;
        debian)
            as_root apt-get update
            as_root apt-get install -y python3 python3-venv python3-pip
            ;;
        fedora)
            as_root dnf install -y python3
            ;;
        suse)
            as_root zypper -n install python3
            ;;
        *)
            printf 'Do not know how to install Python on family %s.\n' "$fam" >&2
            exit 1
            ;;
    esac
}

ensure_venv_module() {
    if python3 -c 'import venv' >/dev/null 2>&1; then
        return 0
    fi
    fam=$(family)
    printf 'The python3 venv module is missing; installing it.\n'
    case $fam in
        debian)
            as_root apt-get install -y python3-venv python3-pip
            ;;
        *)
            printf 'The python3 venv module is not installed.\n' >&2
            exit 1
            ;;
    esac
}

install_project() {
    src=$1
    if "$VENV/bin/python" -c 'import setuptools' >/dev/null 2>&1; then
        "$VENV/bin/python" -m pip install --upgrade --no-build-isolation "$src"
    else
        "$VENV/bin/python" -m pip install --upgrade "$src"
    fi
}

ensure_python
ensure_venv_module

mkdir -p "$SHARE" "$BINDIR" "$APPDIR"
if [ ! -x "$VENV/bin/python" ]; then
    python3 -m venv --system-site-packages "$VENV"
fi
if ! "$VENV/bin/python" -m pip --version >/dev/null 2>&1; then
    "$VENV/bin/python" -m ensurepip --upgrade
fi

if [ -n "$LOCAL" ]; then
    install_project "$LOCAL"
else
    if ! command -v git >/dev/null 2>&1; then
        printf 'git is required to clone %s\n' "$REPO_URL" >&2
        exit 1
    fi
    install_project "git+${REPO_URL}"
fi

if "$VENV/bin/python" -c 'import PySide6' >/dev/null 2>&1; then
    printf 'Using the system PySide6.\n'
else
    printf 'System PySide6 was not found; installing it into the venv.\n'
    "$VENV/bin/python" -m pip install 'PySide6>=6.5'
fi

cat > "$LAUNCHER" << EOF
#!/bin/sh
exec "$VENV/bin/tuxutil" "\$@"
EOF
chmod 755 "$LAUNCHER"

cat > "$DESKTOP" << EOF
[Desktop Entry]
Type=Application
Name=TuxUtil
Comment=Install apps, apply tweaks, and build ISOs
Icon=system-software-install
Terminal=false
Categories=System;Settings;
StartupNotify=true
EOF
printf 'Exec="%s" gui\n' "$LAUNCHER" >> "$DESKTOP"

printf 'Installed %s\n' "$LAUNCHER"
printf 'Desktop entry %s\n' "$DESKTOP"
case ":${PATH}:" in
    *":${BINDIR}:"*) ;;
    *) printf 'Add %s to PATH if your shell does not already include it.\n' "$BINDIR" ;;
esac
