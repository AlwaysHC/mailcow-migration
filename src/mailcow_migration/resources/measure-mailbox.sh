#!/usr/bin/env bash
# Sent over SSH by planning; inspect source messages without using Dovecot's active-only userdb.
set -euo pipefail
export LC_ALL=C
umask 077
directory="$1"
username="$2"
progress_width="${3:-0}"
[[ "$progress_width" =~ ^[0-9]+$ ]] || { printf 'Invalid progress terminal width\n' >&2; exit 1; }
progress_rows=0
render_progress() {
  local line="$1" width offset
  if (( progress_width < 2 )); then printf '%s\n' "$line" >&2; return; fi
  width=$((progress_width - 1))
  if (( progress_rows )); then printf '\033[%dA\r\033[J' "$progress_rows" >&2; fi
  for ((offset=0; offset<${#line}; offset+=width)); do
    printf '%s\n' "${line:offset:width}" >&2
  done
  progress_rows=$(( (${#line} + width - 1) / width ))
}
for tool in find mktemp od gzip wc rm awk; do
  command -v "$tool" >/dev/null || { printf 'Missing source measurement tool: %s\n' "$tool" >&2; exit 1; }
done
for folder in "$directory" "$directory/cur" "$directory/new" "$directory/tmp"; do
  [[ -d "$folder" && ! -L "$folder" ]] || { printf 'Missing or symlinked Maildir: %s\n' "$folder" >&2; exit 1; }
done
work_root="${MAILCOW_WORK_ROOT:-/root/mailcow-in-progress}"
mkdir -p -m 700 -- "$work_root/tmp"
listing="$(mktemp "$work_root/tmp/mailbox-size.XXXXXX")"
trap 'rm -f -- "$listing"' EXIT
find "$directory" \( -type f -o -type l \) -print0 > "$listing"
total=0
while IFS= read -r -d '' file; do
  [[ ! -L "$file" ]] || { printf 'Symlink requires source measurement review: %s\n' "$file" >&2; exit 1; }
  parent="${file%/*}"
  case "${parent##*/}" in cur|new) total=$((total + 1));; esac
done < "$listing"
messages=0
bytes=0
upper_bound=0
metadata_messages=0
stored_bytes=0
previous_percent=-1
# Dovecot preserves original size (S) and optionally IMAP virtual size (W) in compressed Maildir filenames.
# Prefer this metadata for all codecs. Sizing does not validate message contents.
recorded_sizes() {
  local name="${1##*/}" field digits original='' virtual='' fields
  name="${name%%:*}"
  name="${name%Z}"
  IFS=, read -r -a fields <<< "$name"
  for field in "${fields[@]}"; do
    case "$field" in
      S=*|W=*)
        digits="${field#*=}"
        [[ "$digits" =~ ^[0-9]+$ && ${#digits} -le 15 ]] || return 1
        if [[ "$field" == S=* ]]; then
          [[ -z "$original" ]] || return 1
          original=$((10#$digits))
        else
          [[ -z "$virtual" ]] || return 1
          virtual=$((10#$digits))
        fi ;;
    esac
  done
  [[ -n "$original" ]] || return 1
  # Without W, doubling the original bytes bounds the worst case of one inserted CR per original byte.
  if [[ -z "$virtual" ]]; then virtual=$((original * 2)); fi
  (( virtual >= original && virtual <= original * 2 )) || return 1
  printf '%s %s\n' "$original" "$virtual"
}
while IFS= read -r -d '' file; do
  parent="${file%/*}"
  case "${parent##*/}" in cur|new) ;; *) continue ;; esac
  [[ -f "$file" && ! -L "$file" ]] || { printf 'Message changed during measurement: %s\n' "$file" >&2; exit 1; }
  stored="$(wc -c < "$file")"
  [[ "$stored" =~ ^[[:space:]]*[0-9]+[[:space:]]*$ ]] || exit 1
  name="${file##*/}"
  name="${name%%:*}"
  if [[ ",$name" == *,S=* ]]; then
    if ! recorded="$(recorded_sizes "$file")"; then
      printf 'Cannot size message without valid Maildir size metadata: %s\n' "$file" >&2; exit 1
    fi
    read -r size virtual_size <<< "$recorded"
    metadata_messages=$((metadata_messages + 1))
  else
    signature="$(od -An -tx1 -N6 -- "$file")"
    signature="${signature//[[:space:]]/}"
    case "$signature" in
      1f8b*) counts="$(gzip -cd -- "$file" | wc -lc)" ;;
      425a68*|fd377a585a00*|04224d18*|28b52ffd*|446f7665636f*|1f9d*)
        printf 'Unsupported compression (%s) without valid Maildir size metadata: %s\n' "$signature" "$file" >&2
        exit 1 ;;
      *) counts="$(wc -lc < "$file")" ;;
    esac
    read -r lines size <<< "$counts"
    [[ "$lines" =~ ^[0-9]+$ && "$size" =~ ^[0-9]+$ ]] || exit 1
    # IMAP virtual size can add CR before LF. Count every LF conservatively, even if it already has CR.
    virtual_size=$((size + lines))
  fi
  messages=$((messages + 1))
  bytes=$((bytes + size))
  upper_bound=$((upper_bound + virtual_size))
  stored_bytes=$((stored_bytes + stored))
  percent=$((messages * 100 / total))
  if (( percent == previous_percent && messages != total )); then continue; fi
  previous_percent="$percent"
  metrics="$(awk -v percent="$percent" -v original="$bytes" -v stored="$stored_bytes" '
    function human(n) { return n >= 1073741824 ? sprintf("%.2f GiB", n/1073741824) : sprintf("%.2f MiB", n/1048576) }
    function saving(a,b) { return a == 0 ? "N/A" : sprintf("%.2f%%", (1-b/a)*100) }
    BEGIN {
      filled = int(percent / 5)
      printf "[%s%s] %d%% | Total original: %s | Total stored: %s | Total saved: %s",
        substr("####################", 1, filled), substr("--------------------", 1, 20-filled),
        percent, human(original), human(stored), saving(original,stored)
    }
  ')"
  render_progress "Size $username: $messages/$total emails | $metrics"
done < "$listing"
printf 'Mailbox %s totals: %d emails | Original: %s B | Stored: %s B | From size metadata: %d\n' \
  "$username" "$messages" "$bytes" "$stored_bytes" "$metadata_messages" >&2
printf '%s\t%s\t%s\t%s\t%s\n' "$messages" "$bytes" "$upper_bound" "$metadata_messages" "$stored_bytes"
