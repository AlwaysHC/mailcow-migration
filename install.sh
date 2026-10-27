#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: bash install.sh [--python EXECUTABLE] [--install-dir DIRECTORY]

Install Mailcow Migration on the Linux Mailcow host as root.

  --python EXECUTABLE      Python 3.8+ interpreter (default: python3)
  --install-dir DIRECTORY  Installation directory (default: /opt/mailcow-migration)
  --help                   Show this help

The command is installed at /usr/local/bin/mailcow-migrate.
Each installation uses a new virtual environment; previous installations are retained.
Configuration and migration commands are printed after installation.
EOF
}

fail() {
    printf 'Installation stopped: %s\n' "$*" >&2
    exit 1
}

python=python3
install_dir=/opt/mailcow-migration
while (($#)); do
    case "$1" in
        --python|--install-dir)
            (($# >= 2)) && [[ -n "$2" ]] || fail "Missing value for $1"
            if [[ "$1" == --python ]]; then
                python=$2
            else
                install_dir=$2
            fi
            shift 2
            ;;
        --help|-h) usage; exit 0 ;;
        *) fail "Unknown argument: $1 (use --help)" ;;
    esac
done

[[ "$(uname -s)" == Linux ]] || fail 'Run this installer on the Linux Mailcow host.'
((EUID == 0)) || fail 'Run with sudo or from a root shell.'
[[ "$install_dir" == /* ]] || fail '--install-dir must be an absolute path.'
for tool in "$python" install mktemp readlink flock ln mv; do
    command -v "$tool" >/dev/null || fail "Required command is missing: $tool"
done
install_dir=$(readlink -m -- "$install_dir")
[[ "$install_dir" != / ]] || fail 'The installation directory cannot be /.'
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
[[ -f "$source_dir/pyproject.toml" && -d "$source_dir/src/mailcow_migration" ]] ||
    fail 'Keep install.sh in the complete project directory.'

"$python" -c 'import sys; sys.exit("Python 3.8 or newer is required." if sys.version_info < (3, 8) else 0)'
if ! "$python" -c 'import ensurepip, venv'; then
    fail 'Install venv support for the selected Python interpreter (python3-venv on Debian/Ubuntu).'
fi

umask 077
[[ ! -L "$install_dir/releases" ]] || fail 'The releases directory must not be a symlink.'
install -d -m 755 -- "$install_dir" "$install_dir/releases" /usr/local/bin
exec 9>"$install_dir/.install.lock"
flock -n 9 || fail 'Another installation or rollback is running.'

for name in current previous; do
    [[ ! -e "$install_dir/$name" || -L "$install_dir/$name" ]] ||
        fail "$install_dir/$name exists and is not an installation symlink."
done
launcher=/usr/local/bin/mailcow-migrate
launcher_target="$install_dir/current/bin/mailcow-migrate"
if [[ -e "$launcher" || -L "$launcher" ]]; then
    [[ -L "$launcher" && "$(readlink -- "$launcher")" == "$launcher_target" ]] ||
        fail "$launcher already belongs to another installation."
fi

previous_release=
if [[ -L "$install_dir/current" ]]; then
    previous_release=$(readlink -e -- "$install_dir/current") || fail 'The current installation symlink is broken.'
    [[ "$previous_release" == "$install_dir/releases/"* && -x "$previous_release/bin/mailcow-migrate" ]] ||
        fail 'The current installation does not point to a retained release.'
fi

release_dir=$(mktemp -d "$install_dir/releases/install.XXXXXXXX")
printf 'Installing from %s into %s\n' "$source_dir" "$release_dir"
"$python" -m venv "$release_dir"
"$release_dir/bin/python" -m pip --disable-pip-version-check install "$source_dir"
installed_version=$("$release_dir/bin/mailcow-migrate" --version)

switch_link() {
    local name=$1 target=$2
    local temporary="$install_dir/.$name-$$"
    ln -s -- "$target" "$temporary"
    mv -Tf -- "$temporary" "$install_dir/$name"
}

if [[ ! -L "$launcher" ]]; then
    ln -s -- "$launcher_target" "$launcher"
fi
if [[ -n "$previous_release" ]]; then
    switch_link previous "$previous_release"
fi
switch_link current "$release_dir"

printf '\nInstalled %s\nCommand: %s\nRelease: %s\n' "$installed_version" "$launcher" "$release_dir"
if [[ -n "$previous_release" ]]; then
    printf 'Previous release retained: %s\n' "$previous_release"
fi
cat <<'EOF'

Next steps, in a root shell:
  mailcow-migrate configure --config /root/mailcow-in-progress/migration.json
  mailcow-migrate check-config --config /root/mailcow-in-progress/migration.json
  mailcow-migrate prepare --config /root/mailcow-in-progress/migration.json

If configuration already exists, skip configure and run check-config and prepare.
See docs/installation.md for credentials, guided preparation, updates and rollback.
EOF
