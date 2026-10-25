#!/usr/bin/env bash
set -euo pipefail
umask 077
root="$1"
identifier="$2"
[[ "$root" == /root/mailcow-in-progress && "$identifier" =~ ^[a-f0-9]{32}$ ]] || {
  printf 'Invalid export staging location.\n' >&2
  exit 2
}
for directory in /root "$root" "$root/scripts" "$root/exports"; do
  if [[ -L "$directory" || -e "$directory" && ! -d "$directory" ]]; then
    printf 'Unsafe export directory: %s\n' "$directory" >&2
    exit 2
  fi
  if [[ ! -d "$directory" ]]; then mkdir -m 700 -- "$directory"; fi
done
mkdir -m 700 -- "$root/scripts/python-$identifier"
