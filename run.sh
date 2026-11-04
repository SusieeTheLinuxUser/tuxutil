#!/bin/sh

fail() {
    printf 'TuxUtil: %s\n' "$*" >&2
    exit 1
}

check_requirements() {
    command -v python3 >/dev/null 2>&1 || fail 'python3 3.10 or newer is required.'
    python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' 2>/dev/null ||
        fail 'Python 3.10 or newer is required.'
    if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
        fail 'curl or wget is required to download TuxUtil.'
    fi
    command -v tar >/dev/null 2>&1 || fail 'tar is required to extract TuxUtil.'
}

download() {
    url=$1
    output=$2
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$url" -o "$output"
    else
        wget -q "$url" -O "$output"
    fi
}

prepare_app() {
    cache_base=${XDG_CACHE_HOME:-$HOME/.cache}/tuxutil
    app_dir=$cache_base/app
    mkdir -p "$cache_base" || fail "cannot create cache directory: $cache_base"
    work=$(mktemp -d "$cache_base/.download.XXXXXX") || fail "cannot create temporary folder in $cache_base"
    trap 'rm -rf "$work"' EXIT
    trap 'rm -rf "$work"; exit 130' INT TERM
    url=${TUXUTIL_TARBALL:-https://github.com/SusieeTheLinuxUser/tuxutil/archive/refs/heads/main.tar.gz}

    if download "$url" "$work/source.tar.gz"; then
        if ! tar -xzf "$work/source.tar.gz" --strip-components=1 -C "$work" 2>/dev/null ||
            [ ! -f "$work/tuxutil/__main__.py" ]; then
            rm -rf "$work"
            if [ -f "$app_dir/tuxutil/__main__.py" ]; then
                printf '%s\n' 'TuxUtil: download was invalid; using the cached copy.' >&2
                return 0
            fi
            fail 'download did not contain a valid TuxUtil source tree.'
        fi
        rm -f "$work/source.tar.gz"
        old_dir=$cache_base/.old-app
        rm -rf "$old_dir"
        if [ -d "$app_dir" ]; then mv "$app_dir" "$old_dir" || fail 'cannot move cached app aside.'; fi
        if mv "$work" "$app_dir"; then
            rm -rf "$old_dir"
        else
            [ ! -d "$old_dir" ] || mv "$old_dir" "$app_dir"
            rm -rf "$work"
            fail 'could not install downloaded source; kept the previous cached copy.'
        fi
    elif [ -f "$app_dir/tuxutil/__main__.py" ]; then
        rm -rf "$work"
        printf '%s\n' 'TuxUtil: download failed; offline fallback is using the cached copy.' >&2
    else
        rm -rf "$work"
        fail 'download failed and no cached copy is available.'
    fi
    trap - EXIT INT TERM
}

run_app() {
    cache_base=${XDG_CACHE_HOME:-$HOME/.cache}/tuxutil
    app_dir=$cache_base/app
    python_bin=python3
    is_gui=0
    if [ "$#" -eq 0 ] || [ "$1" = gui ]; then is_gui=1; fi

    if [ "$is_gui" -eq 1 ] && ! python3 -c 'import PySide6.QtWidgets' >/dev/null 2>&1; then
        venv_python=$cache_base/venv/bin/python
        if [ ! -x "$venv_python" ] && ! python3 -m venv --system-site-packages "$cache_base/venv"; then
            # A failed venv leaves a python behind, which the next run would wrongly reuse.
            rm -rf "$cache_base/venv"
            printf '%s\n' 'TuxUtil: could not create its GUI venv; on Debian or Ubuntu install python3-venv. The CLI still works.' >&2
            return 1
        fi
        venv_python=$cache_base/venv/bin/python
        if ! "$venv_python" -c 'import PySide6.QtWidgets' >/dev/null 2>&1; then
            printf '%s\n' 'TuxUtil: first GUI run: downloading PySide6-Essentials (about 85 MB, 260 MB on disk) to the cache...' >&2
            if ! "$venv_python" -m pip install PySide6-Essentials; then
                printf '%s\n' 'TuxUtil: GUI dependency installation failed; the CLI still works.' >&2
                return 1
            fi
        fi
        python_bin=$venv_python
    fi

    if [ -r /dev/tty ] && ( : </dev/tty ) 2>/dev/null; then
        PYTHONPATH=$app_dir "$python_bin" -m tuxutil "$@" </dev/tty
    else
        PYTHONPATH=$app_dir "$python_bin" -m tuxutil "$@" </dev/null
    fi
}

main() {
    check_requirements
    prepare_app
    run_app "$@"
}

main "$@"
