#!/usr/bin/env bash
# Execute with bash. Do not source into the SSH login shell.
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  printf 'Execute this script with bash; do not source it.\n' >&2
  return 2
fi
set -euo pipefail
umask 077
trap 'printf "Export stopped at line %s. Existing files were retained; this export is incomplete.\n" "$LINENO" >&2' ERR

usage() {
  printf 'Usage: bash export-domain.sh DOMAIN|--server OUTPUT_DIR [MYSQL_OPTION_FILE] [DATABASE] [MAIL_ROOT]\n'
  printf 'Example: bash export-domain.sh example.org /root/mailcow-in-progress/exports/example.org /root/.my.cnf postfix /var/posta\n'
}
completed=0
steps=10
step() {
  completed=$((completed + 1))
  local bar='' i
  for ((i=0; i<20; i++)); do
    if (( i < completed * 20 / steps )); then bar+='#'; else bar+='-'; fi
  done
  printf 'Export [%s] %d%% %d/%d %s\n' "$bar" "$((completed * 100 / steps))" "$completed" "$steps" "$1" >&2
}
if [[ "${1:-}" == '--help' ]]; then usage; exit 0; fi
if (( $# < 2 || $# > 5 )); then usage >&2; exit 2; fi
domain="$1"
scope=domain
if [[ "$domain" == '--server' ]]; then scope=server; steps=9; fi
output="$2"
work_root="${MAILCOW_WORK_ROOT:-/root/mailcow-in-progress}"
[[ "$(readlink -m -- "$output")" == "$(readlink -m -- "$work_root")/"* ]] || {
  printf 'Export output must be below %s\n' "$work_root" >&2; exit 2;
}
options="${3:-}"
database="${4:-postfix}"
mail_root="${5:-/var/posta}"
if [[ "$scope" == domain ]]; then
  [[ "$domain" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ && "$domain" == *.* && "$domain" != *..* ]] || {
    printf 'Invalid domain: %s\n' "$domain" >&2; exit 2;
  }
fi
[[ "$database" =~ ^[a-zA-Z0-9_]+$ && "$output" == /* && "$mail_root" == /* ]] || {
  printf 'Database name must contain only letters, digits or underscore; output and mail root must be absolute paths.\n' >&2; exit 2;
}
for executable in mysql mysqldump postconf postqueue dovecot doveconf tar cp awk find wc; do
  command -v "$executable" >/dev/null || { printf 'Missing command: %s\n' "$executable" >&2; exit 2; }
done
# Older Bash treats an empty array as unset under nounset. Keep both command arrays nonempty.
mysql_command=(mysql)
dump_command=(mysqldump)
if [[ -n "$options" ]]; then
  [[ -f "$options" ]] || { printf 'MySQL option file not found on the source: %s\n' "$options" >&2; exit 2; }
  mysql_command+=("--defaults-extra-file=$options")
  dump_command+=("--defaults-extra-file=$options")
fi
query='SELECT 1 FROM admin LIMIT 1'
if [[ "$scope" == domain ]]; then query="SELECT COUNT(*) FROM domain WHERE domain='$domain'"; fi
if ! count="$("${mysql_command[@]}" -N -B "$database" -e "$query")"; then
  printf 'MySQL connection or configuration query failed; no export directory was created.\n' >&2
  printf 'Linux root does not automatically authenticate as MySQL root.\n' >&2
  printf 'Pass a protected MySQL [client] option file as argument 3; prepare-server.sh explains how to create it.\n' >&2
  printf 'Example: bash export-domain.sh %s %s /root/migration-mysql.cnf\n' "$domain" "$output" >&2
  exit 1
fi
if [[ "$scope" == domain ]]; then
  [[ "$count" == '1' ]] || { printf 'Primary domain not found: %s\n' "$domain" >&2; exit 2; }
  count="$("${mysql_command[@]}" -N -B "$database" -e "SELECT COUNT(*) FROM alias_domain WHERE alias_domain='$domain'")"
  [[ "$count" == '0' ]] || { printf '%s is an alias domain; select its primary instead.\n' "$domain" >&2; exit 2; }
fi
[[ ! -e "$output" ]] || { printf 'Output already exists. Choose a new directory; nothing overwritten.\n' >&2; exit 2; }
mkdir -p -m 700 -- "$(dirname -- "$output")"
mkdir -m 700 -- "$output"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
[[ -r "$script_dir/dump-progress.awk" ]] || { printf 'Keep dump-progress.awk beside this script.\n' >&2; exit 2; }
printf 'Export [--------------------] 0%% 0/%d Counting %s configuration rows...\n' "$steps" "$scope" >&2
tables=(admin alias alias_domain domain domain_admins fetchmail mailbox vacation)
if [[ "$scope" == server ]]; then tables=(admin); fi
query=''
for table in "${tables[@]}"; do
  query+="${query:+ UNION ALL }SELECT '$table', COUNT(*) FROM $table"
done
"${mysql_command[@]}" -N -B "$database" -e "$query" > "$output/row-counts.tsv"
total=0
while read -r table count; do
  [[ "$count" =~ ^[0-9]+$ ]] || { printf 'Invalid row count.\n' >&2; exit 2; }
  total=$((total + count))
  printf '  %s: %s rows\n' "$table" "$count" >&2
done < "$output/row-counts.tsv"
printf 'Counted %d configuration rows across %d tables. Starting dump...\n' "$total" "${#tables[@]}" >&2

# Domain exports need all configuration tables to resolve cross-domain dependencies; server exports need only admin.
# Explicit options fix the SQL parser's input format, including on older MySQL clients.
"${dump_command[@]}" --single-transaction --tz-utc --default-character-set=utf8mb4 \
  --skip-hex-blob --skip-complete-insert --skip-compact --skip-extended-insert --create-options --quote-names \
  "$database" "${tables[@]}" | awk -v total="$total" -v count_file="$output/dumped-row-count.txt" \
    -f "$script_dir/dump-progress.awk" > "$output/source-settings.sql"
actual="$(< "$output/dumped-row-count.txt")"
printf 'SQL rows [####################] 100%% %s/%s dumped\n' "$actual" "$actual" >&2
if [[ "$actual" != "$total" ]]; then
  printf 'Row count changed between pre-count (%s) and dump (%s); review concurrent settings changes.\n' "$total" "$actual" >&2
fi
step 'SQL dump complete'
postconf mail_version > "$output/postfix-version.txt"
dovecot --version > "$output/dovecot-version.txt"
step 'Server versions saved'
postconf -n > "$output/postfix-effective.conf"
step 'Postfix settings saved'
config_dir="$(postconf -h config_directory)"
cp -p -- "$config_dir/master.cf" "$output/postfix-services.conf"
step 'Postfix services saved'
doveconf -n > "$output/dovecot-effective.conf"
step 'Dovecot settings saved'
postqueue -p > "$output/postfix-queue.txt"
step 'Queue inventory saved'
printf 'Counting server configuration files...\n' >&2
files="$(find /etc/postfix /etc/dovecot -type f -printf '.' | wc -c)"
printf 'Archiving %s regular configuration files (plus directories and links)...\n' "$files" >&2
tar -czf "$output/server-config.tgz" -C / etc/postfix etc/dovecot
step 'Server configuration archive complete'
mkdir -m 700 -- "$output/config"
maps=(transport sender_access rbl_override)
if [[ "$scope" == domain ]]; then maps+=(virtual copia_posta_uscita); fi
for map in "${maps[@]}"; do
  if [[ -f "$config_dir/$map" ]]; then cp -p -- "$config_dir/$map" "$output/config/$map"; fi
done
if [[ "$scope" == domain ]]; then
  mkdir -m 700 -- "$output/sieve"
  cp -p -- /etc/dovecot/sieve.conf "$output/config/default.sieve"
fi
step 'Configuration maps saved'
if [[ "$scope" == domain ]]; then
  : > "$output/config/inactive-sieve.files"
  if [[ -d "$mail_root/sieve/$domain" ]]; then
    printf 'Counting personal Sieve files...\n' >&2
    files="$(find -L "$mail_root/sieve/$domain" -type f -printf '.' | wc -c)"
    printf 'Copying %s personal Sieve files...\n' "$files" >&2
    # Dereference active links on the source; target never follows an old absolute symlink.
    cp -aL -- "$mail_root/sieve/$domain" "$output/sieve/$domain"
    find -L "$mail_root/sieve/$domain" -type f -name '*.sieve' -print0 > "$output/config/sieve-files"
    while IFS= read -r -d '' script; do
      relative="${script#"$mail_root/sieve/$domain/"}"
      active="$mail_root/sieve/$domain/${relative%%/*}/.dovecot.sieve"
      resolved="$(readlink -f -- "$script")"
      if [[ ! -f "$active" ]] || [[ "$resolved" != "$(readlink -f -- "$active")" ]]; then
        printf '%s\0' "${script#"$mail_root/sieve/"}" >> "$output/config/inactive-sieve.files"
      fi
    done < "$output/config/sieve-files"
  fi
  step 'Personal Sieve copy complete'
  printf '%s\n' "$domain" > "$output/domain.txt"
  printf '%s\n' "$mail_root" > "$output/mail-root.txt"
fi
printf '%s\n' "$scope" > "$output/scope.txt"
printf 'complete\n' > "$output/COMPLETE"
step 'Export marked complete'
printf '%s configuration export complete: %s\nContains configuration credentials; keep private. No emails copied.\n' "$scope" "$output"
