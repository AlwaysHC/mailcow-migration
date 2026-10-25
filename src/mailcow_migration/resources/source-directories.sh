#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$1" && pwd -P)"
domain="$2"
subdir="$3"
shift 3
addresses="$(hostname -I)"
printf 'addresses\t%s\n' "$addresses"
if [[ -d "$root/$domain" && ! -L "$root/$domain" ]]; then printf 'directory\t1\n'; else printf 'directory\t0\n'; fi
for user in "$@"; do
  mailbox_home="$root/$domain/$user"
  maildir="${mailbox_home}${subdir:+/$subdir}"
  valid=1
  for directory in "$root" "$root/$domain" "$mailbox_home" "$maildir" "$maildir/cur" "$maildir/new" "$maildir/tmp"; do
    if [[ ! -d "$directory" || -L "$directory" ]]; then valid=0; fi
  done
  printf 'mailbox\t%s\t%s\n' "$user" "$valid"
done
