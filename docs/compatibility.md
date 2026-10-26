# Compatibility and dependencies

## Support policy

Compatibility depends on source schemas, effective configuration and available capabilities, not product names alone.
This document describes the initial environment requirements and the capabilities needed by planned migration workflows.
It is not an end-to-end certification of the listed product versions.

Configuration, source inspection, private domain exports, planning and inactive account staging are implemented.
Target isolation and PostfixAdmin login and delivery controls are available separately.
RoundCube snapshots, SOGo proposals and session previews support either source; revocation requires the PostfixAdmin isolation path.
SOGo configuration preparation, native user-data conflict inspection and target folder-state assessment are available separately from import.
Explicit SOGo user preparation can reload missing domain user sources and reconcile selected cached unknown-user lookups.
The PostfixAdmin webmail command connects RoundCube import, editable filters and native vacation/forwarding settings with readback.
Guarded PostfixAdmin account activation is available and includes staged prefilter activation under verified isolation.
Quota recalculation follows activation for planned active mailboxes, with copied-volume and isolation checks.
Guarded append-only mail copying is available for PostfixAdmin batches with explicit stopped-writer confirmation.
Personal/default Sieve installation and compilation are available for PostfixAdmin batches under the same transfer guards.
Inactive forwarding/vacation prefilter staging is available separately, with exact API readback and uncertain-request reconciliation.
Explicit sender-permission staging and read-only verification are also available.
Plesk import/isolation, full source isolation and full recovery remain planned work.
The configuration wizard writes versioned JSON that
supplies source/target settings through a shared loader.
PostfixAdmin and Plesk are independently optional: configure at least one. Source-specific dependencies and checks apply
only to configured sources; an unconfigured server does not need to exist or be reachable.
The `inspect` command uses local OpenSSH and `dig`, remote Bash/`hostname -I` and filesystem access, plus the PostfixAdmin
MySQL client or Plesk Python/CLI database access. It checks source inventories and current authoritative DNS routing;
full prerequisite assessment and migration-resume checks remain planned work.
`plan` provides initial target prerequisites, domain/mailbox/alias/BCC conflicts, mail volume and vmail identity inspection,
rsync capability checks and read-only append previews. This does not authorize transfer or establish journal-aware resume.
It uses completed exports, parses source
account data and calculates quotas from mailbox measurements. It also assesses source Sieve, vacation and forwarding mappings.
PostfixAdmin planning assesses shared routing/policy maps and per-service sender restrictions from the exported configuration.
It does not resume existing migration journals, verify shared-setting application or establish target webmail readiness.
The `export` command additionally uses local `scp -O` for PostfixAdmin and the existing source Bash/MySQL dump/configuration
utilities; Plesk export uses its root-only Python/CLI snapshot reader. Only the matching source is exported. Export success
does not qualify account mappings or product versions, and migration-resume support is still pending.
Default planning also requires the source sizing helper's Bash/find/mktemp/od/gzip/wc/rm/awk commands, and checks current source
inventory before and after sizing. Its PostfixAdmin `--quota-mib` override skips source measurement and source transfer checks;
the live target and local receiver capabilities are still checked. Transfer checks require readable host access to the actual
Dovecot mail-volume mount and a positive numeric vmail UID/GID. Sender rsync options remain separate from receiver-only
ownership and progress capabilities. No mailbox files are changed by the checksum preview; actual copying still requires
staged accounts, matching journals, source/target write isolation and a pre-activation state.
PostfixAdmin vacation assessment uses the host's IANA timezone data, normally under `/usr/share/zoneinfo` (`TZDIR` may select
the system data directory). The bundled Python helper resolves date parts in a separate process, retaining Python 3.8 support
without changing the CLI process timezone or adding a runtime package. Unknown zones and missing timezone data stop planning.
Plesk Sieve is prepared for copying; its scripts are not restricted to the subset accepted by the editable SOGo filter mapper.

`accounts` uses a completed saved plan and matching configuration. Preview and `verify` require API inventory access;
`accounts --apply` requires API write access. Pending writes are reconciled only from observable target values, using
Mailcow's domain quota field mapping, mailbox attributes, native alias behavior and recovery-email support.
Password reconciliation additionally requires Docker Compose and the running `php-fpm-mailcow` container with its application
configuration, PDO database access, `hash_equals` and `verify_hash`. This read-only helper does not expose target password hashes.
The account journal format supports resuming this application's staging operations, not arbitrary external migration journals.

`target-isolation` requires completed account staging, root access on the Mailcow host, Docker Compose, readable host `/proc`
and cron inventories, and running Postfix, Dovecot, nginx and SOGo containers. Dovecot needs `doveadm` and Python 3 for identifying
running imapsync processes. The Mailcow API must expose all seven mailbox protocol controls and valid sync-job ownership/activity.
Pause/release additionally need API writes and writable Postfix/nginx configuration directories. Postfix service overrides of
`transport_maps`, Dovecot replication and non-loopback published SOGo ports block the supported isolation path.
nginx must load `/etc/nginx/conf.d/site.*.custom`; HTTPS checks retain certificate verification and refuse redirects.
Restoration remains gated on completed activation, quota recalculation and verified webmail import.

## Initial compatibility matrix

| Component | Existing boundary or agreed runtime | Required configuration |
| --- | --- | --- |
| Python core | CPython 3.8 minimum | Use syntax, APIs, and any package versions compatible with 3.8. |
| Host operating system | Linux | Local Mailcow installation, root access, Docker and source SSH access. |
| PostfixAdmin source | Existing schema adapter; no product-release gate | Preserve the SQL and mail-server checks below. |
| Plesk source | Documented Obsidian 18.0.81.2 schema | Preserve Dovecot 2.4.x and the existing Postfix isolation checks. |
| Mailcow target | Existing API/container integration; no product-release gate | Preserve API, configuration, storage, and container checks. |
| RoundCube source | 1.6.x exporter boundary | MySQL-backed user data and PHP CLI 7.3 or later with `pdo_mysql`. |
| SOGo target | Existing Mailcow image integration | Preserve preference, contact, and Sieve capability checks. |

Python 3.8 is chosen to accommodate Ubuntu 20.04's system interpreter, documented in the
[Ubuntu release notes](https://documentation.ubuntu.com/release-notes/20.04/#python3-by-default).
This interpreter baseline does not qualify every Mailcow or Plesk release on Ubuntu 20.04.

Planned Plesk isolation targets Postfix major versions matching `[3-9]`, with additional SASL and configuration checks.
That version boundary does not establish compatibility with every current or future release.

## Source capabilities

**PostfixAdmin:** MySQL/MariaDB access to `domain`, `mailbox`, `alias`, `alias_domain`, `vacation`, `admin`,
`domain_admins`, and `fetchmail`, including the fields consumed by the adapter. Planned prerequisite checks cover
MySQL clients from 5.5.3 or MariaDB clients from 5.5, Bash from 3.2, and Dovecot from 2.0. These are command baselines,
not supported database or mail-server release ranges.

`source-logins` requires one SQL passdb at `/etc/dovecot/dovecot-mysql.conf`, with a recognized
query and account layout. Supporting additional layouts is future work.
The source needs PHP CLI with `JSON_THROW_ON_ERROR`, `cp --preserve=all`, `doveconf` and `doveadm`,
plus permission to reload Dovecot and edit its SQL configuration. That configuration must be a regular single-link file
without symlinks. Full backups and database credentials remain on the source; only the query and verification hashes return.
Before freeze, source routing and the saved RoundCube snapshot/proposal must verify, with review, required deferrals and browser drafts confirmed.
Required SOGo target configuration must also pass live preflight; incomplete configuration/restart records block further isolation.
Login release remains available when those checks fail.
`source-delivery` requires one primary domain, a safe `/root/mailcow-in-progress` directory from source preparation,
Postfix retry/error transport, hash/regexp maps, `postconf`, `postmap`, `postfix`, `postqueue` and `postcat`, and readable cron/`/proc` data.
Only hash BCC maps beneath `/etc/postfix` are adapted; global `always_bcc`, incompatible service overrides, linked domains and
recipient delimiters other than empty or `+` block this path. Queue inspection selects `postcat -e` for Postfix 2.7 or later,
otherwise parses `postcat -v` locally without retaining message content. Its delivery pause is not full source isolation;
RoundCube sessions are revoked after delivery holds. Combined submission checks and guarded forwarding/release remain pending.
Postfix commands and configuration must permit scoped submission/delivery holds, queue inspection, and source forwarding.
Mailboxes must use the inspected Maildir layout. A resolvable mail-root symlink is allowed; unsafe mailbox/message links block copying.

**Plesk:** `plesk db --xml` and `plesk bin mailserver`, the expected domain/account/alias/service schema, and access to
`PLESK_MAILNAMES_D` from `/etc/psa/psa.conf`. Mailboxes use `DOMAIN/USER/Maildir`. Python source helpers must work on 3.8.
Credentials stored in Plesk's symmetric format require its `mail_auth_view` utility to produce complete usable values.
Isolation requires Dovecot 2.4 configuration syntax, full email-address logins, Postfix regexp maps, and a supported
Cyrus or Dovecot SASL setup. Forwarding additionally needs `ip -j address show`, original-recipient tracking,
an empty or `+` recipient delimiter, and a compatible routing/service configuration.

**RoundCube:** Readable installation configuration, MySQL DSN, recognized schema, and InnoDB user-data tables for a
consistent snapshot. Session revocation requires database-backed sessions and RoundCube's session decoder.
The source also needs `sh`, `mktemp`, `cat` and `rm` to transfer and clean up a private, complete PHP script file; PHP receives
no SSH input after transfer. Configuration `exit`/`die`, malformed JSON and incomplete snapshots stop the operation.
The selected installation and snapshot are pinned to the batch, and SQL credentials remain in the source configuration.
Session deletion uses stored user ownership plus an ID/value comparison; unexpected ownership or concurrent changes stop it.
Unknown tables, shared dictionaries and selected-profile groups, responses, dictionaries, searches and files block source
isolation until their preservation is supported. Supported contacts, identities and preferences are mapped into private proposals;
target configuration, conflict checks and import remain pending. Named timezones use the host IANA timezone data through the
existing isolated Python helper; validation does not change the CLI process timezone.
Collapsed-folder mapping requires Dovecot's JSON mailbox list in modified UTF-7, an empty personal namespace prefix and a
verified `.` or `/` separator. Its default is derived from the supported Dovecot 2.x storage layout when not explicitly configured.
Unknown or missing folder evidence blocks the source proposal; target folder verification must happen after message copying.
Review and explicit receipt/reply-folder deferrals are bound to the unchanged proposal fingerprint, not to upstream feature status.
The recognized plugin set is `archive`, `zipdownload`, `managesieve`, `emoticons`, `password`, and `markasjunk`;
recognition does not imply that every plugin state can be converted. Unknown data, pending mandatory password changes,
per-host configuration, or external address books require explicit handling before migration.

## Target capabilities

The target must provide an HTTPS Mailcow API, a usable API key with the required read/write access, local configuration
and mail-volume access, and Docker Compose v2. Verify `MAILDIR_SUB=Maildir`, vmail ownership, and the actual Docker mounts.

`senders` requires mailbox/alias inventories exposing `sender_acl`, alias `sender_allowed`/destinations, and mailbox activity.
Applying a merge requires `edit/mailbox` and an observable security-attributes dictionary; current `force_tfa` and
`force_pw_update` values are included in the edit and read back. Target ACL entries must be strings. Sender mappings come
only from the saved policy/account proposal. Preview/apply require staged inactive accounts; `--check` performs no writes
and can verify completed permission staging after activation. This does not certify SMTP service restriction behavior.

`prefilters` requires `get/filters/all` and, when applying, `add/filter`, plus the account inventories used for staging checks.
Filter rows must expose their username, type, description, exact script, activity and a usable ID. The command creates only
inactive prefilters and requires the saved PostfixAdmin export/policy/webmail proposal. It does not require a stopped-writer
flag because it does not enable filters or change mail services. Native SOGo settings and guarded activation remain separate.

The prefilter activation backend uses the same inventories plus `edit/filter` activity updates, retaining IDs recorded
at staging. It requires final mail/Sieve receipts and matching evidence fingerprints. It honors verified native SOGo handoffs,
blocks uncertain native writes, and reconciles uncertain activation requests through exact target readback. The `activate` command
enforces account-activation order, stopped writers and source/target isolation while holding the shared lock.

`activate` additionally requires `edit/domain`, `edit/mailbox`, `edit/alias-domain`, `edit/alias` and `edit/bcc` for planned objects.
Aliases and BCC records must expose unique, stable IDs. Only activity fields are edited; target protocol/delivery restrictions
remain paused. Final mail/Sieve receipts, recovery emails and sender permissions must verify before activation. Partial runs pin
their ordered requests and validate activity against completed/pending receipts. The separate `verify` and target isolation check
commands support these phases. Activation does not complete webmail import, recalculate quota or authorize isolation release.

`quota` requires the activated PostfixAdmin batch, an active primary domain, completed mail/Sieve receipts and the original
`/var/vmail` host mount. It runs `doveadm quota recalc -u USER` only for planned active users, streaming native output and retaining
an incomplete receipt on failure/interruption. There is no command timeout. Completion requires successful native exits and
renewed readiness checks, not independently measured usage values. Disabled users require controlled maintenance before later activation.
Webmail import and target release require the matching quota receipt and live volume; neither repeats recalculation automatically.

`sogo-target` requires the running `sogo-mailcow` container, its `sogo` user, `sogo-tool`, `plutil`, and a readable regular
`data/conf/sogo/sogo.conf` without symlinked paths. Applying custom identity support also needs writable host configuration,
permission to restart SOGo and `nc` in the container for its startup check. Native user inspection needs loaded SOGo user sources
and working per-user backups; inactive staging alone does not prove those prerequisites. Property-list output uses the supported
GNUstep representation, including Unicode escapes, and is parsed before applying a proposed configuration change.
The `preview` and `inspect-users` actions do not clear cached user lookups, refresh sources, import user data or grant address-book ownership.

`sogo-target --action prepare-users --apply` additionally requires PHP CLI with `JSON_THROW_ON_ERROR` and the `Memcached`
extension in `php-fpm-mailcow`, plus a nonempty effective `SOGoMemcachedHost`. The helper accepts comma-separated hosts with
optional ports, defaulting to 11211, and rejects unsupported URL components. It deletes only selected `username+attributes`
entries whose string value trims to `null`; cache failures remain visible. Missing user sources trigger the shared SOGo
restart/startup check, so restart permission and `nc` are also required. Domain activation and valid source definitions must
already allow these users to resolve. Preparation retains private attempt records and finishes with native conflict checks.

Native user inspection rechecks the saved RoundCube snapshot and source folder trees through the configured SSH/PHP connection.
For profiles with folder display state, it also requires `doveadm`, `doveconf` and, when deriving an unset separator, `dovecot`
inside `dovecot-mailcow`. The supported personal namespace has an empty prefix and a `.` or `/` hierarchy separator; every
source folder must already exist on the target. SOGo backups must contain a user-settings dictionary, with a dictionary-valued
`Mail` entry when present. `ExpandedFolders`, when present, must be an array or JSON-encoded array of supported account/folder
identifiers with the expected primary-account selection. Folder checks establish display-state compatibility only.

The native import backend uses `sogo-tool user-preferences set` with a private JSON file for defaults/settings, and additive
`sogo-tool restore -f` for address-book records. It requires the same native backup/conversion capabilities as inspection.
Temporary files run as the `sogo` user with owner-only permissions; failures retain them for recovery. This backend has not
been validated end to end. The `webmail` command enforces completed transfers, activation, approved source proposals and live
source/target isolation. Its preview retains private backups without importing user data; native settings target checks are
deferred until editable-filter import is complete. Preparation and shared-service restarts remain separate explicit actions.

Target release requires a full `mailcow-webmail-import` receipt bound to the batch/source/target, exact completion receipts
from all three native stages, and completed quota recalculation on the copied volume. A component's completion flag alone is
insufficient. Completed webmail reruns validate receipt integrity and retain later user edits; webmail import never releases
access or changes DNS/source routing automatically.

| Container | Capabilities needed by planned migration workflows |
| --- | --- |
| `dovecot-mailcow` | `doveadm`, `doveconf`, `sievec`, Python 3, quota/Sieve operations, and readable process information. |
| `postfix-mailcow` | Configuration lookup, map creation, queue inspection, validation, and reload commands. |
| `sogo-mailcow` | `sogo-tool`, `plutil`, contacts/preferences access, and enabled Sieve features where selected. |
| `php-fpm-mailcow` | PHP helpers using the installed Mailcow application and database configuration. |
| `nginx-mailcow` | Configuration validation/reload and the expected webmail access controls. |
| `acme-mailcow` | Certificate provisioning when the operator selects the certificate setup feature. |

The installed SOGo behavior must be checked before mapping preferences. Upstream pull-request approval or merge does not
prove that a deployed image contains a feature. Other target services are prerequisites of the existing Mailcow installation.

## External commands and access

These command dependencies cover the available commands and planned migration workflows.
Compatibility checks must verify options and behavior, not just executable presence.

The `mail` command runs as root on the Linux Mailcow host and requires completed account staging, source login/delivery/session
isolation and target access/delivery isolation. Plesk copying is blocked until its isolation adapter is implemented.
It requires GNU `install` for owned directories, `MAILDIR_SUB=Maildir`, local mail-volume access and rsync support for archive/
hard-link preservation, safe links, checksums, itemized dry runs, ownership mapping, `--ignore-existing`, statistics and progress.
Both rsync endpoints are probed. SSH retains strict host-key checking and the configured connection file.
Progress is streamed to the terminal; inspection/count diagnostics are private. Interrupted copies retain their transfer journal.
The required `--writers-stopped` confirmation covers submission, hidden custom writers and account administration that automatic
checks cannot fully establish. A final mail pass does not certify Sieve, quota recalculation, webmail import or full cutover.

The `sieve` command additionally requires the completed planning export, an existing regular Sieve directory inside the mail
volume, and effective Dovecot `plugin/sieve` equal to `/var/vmail/sieve/%u.sieve`. It uses GNU `install` for mode/ownership and
`sievec` as the container's `vmail` user. Exported scripts must pass the supported PostfixAdmin syntax/mapping checks; compiled
files must remain regular files. Preview never invokes the compiler or changes target scripts. Applied compilation failures
retain pending state and recovery backups; shared defaults, native SOGo preferences and account activation are separate.

The internal editable SOGo filter backend requires the same active Sieve path, plus `doveadm sieve list/put/activate` and
`doveadm -f json sieve get` returning one record with a string `sieve script` field. SOGo must expose configured user sources,
editable-filter support and the enabled capabilities needed by the converted rules. Its native backups must contain two
preference dictionaries. `sogo-tool restore -p` must accept XML property lists and retain unrelated defaults/settings.
The backend verifies both stored and active script contents and preserves private pending receipts on failure. The `webmail`
command supplies completed transfer, activation and isolation checks with stopped writers.

The native vacation/forwarding backend additionally requires `date -d @SECONDS "+%F %T"` in both SOGo and Dovecot containers,
with results matching the policy timezone at each vacation boundary. Native preference imports require readable
`/etc/sogo/sieve.creds` for the SOGo user and support for `sogo-tool user-preferences set ... -p /etc/sogo/sieve.creds -F` when
generating delivery scripts. Active vacations must generate the expected inclusive `currentdate` date guards. Prefilter handoff
uses `get/filters/all` and `edit/filter` only for a matching staged filter; readback verifies its ID, owner, type, body, label and
activity. Pending settings writes require reconciliation before another attempt. This is the final stage of `webmail --apply`.

| Location | Commands or facilities |
| --- | --- |
| Mailcow host | Docker/Compose, OpenSSH `ssh`/`scp`, `rsync`, `dig`, Bash, `awk`, and standard filesystem utilities. |
| Locking | POSIX `flock` coordinates operations through a shared lock file. |
| Source hosts | SSH service, Bash, `rsync`, `hostname -I`, GNU coreutils/findutils/tar, `gzip`, and `awk`. |
| PostfixAdmin source | `mysql`, `mysqldump`, Postfix/Dovecot commands, and PHP CLI for configuration/isolation helpers. |
| Plesk source | Python, Plesk utilities, Postfix/Dovecot commands, and `ip` for forwarding. |
| RoundCube source | PHP CLI and `pdo_mysql`, plus installation files and database credentials. |

The existing export uses `scp -O`; preserve that capability requirement unless an equivalent implementation replaces it.
The receiver uses rsync ownership, append-only, checksum, and progress options including `--chown`, `--ignore-existing`,
and `--info=progress2`. Source and receiver checks must distinguish their required options.

Require configured SSH authentication and verified host keys, database access on the source, DNS resolution and
authoritative queries, and reachability of the Mailcow API and selected mail endpoints. Handoff checks cover SMTP 25,
HTTPS 443, IMAPS 993, POP3S 995, SMTPS 465, and STARTTLS submission 587 under the initial service profile.
Configurable service profiles are future work.

GitHub API access is optional and informational for upstream SOGo/SOPE feature status. A GitHub token is optional.
Node.js and npm are not dependencies of the new application. The foundation uses only the Python standard library at
runtime. Installation uses setuptools with a Python 3.8-compatible version constraint. Any later dependency must support
the chosen interpreter baseline.

## Deferred compatibility work

Additional product releases, broader database/authentication/storage support, and new configuration profiles are future work.
See the [feature roadmap](features.md) for the distinction between implemented capabilities and planned workflows.
