# Mailcow Migration

A Python CLI for preparing mail-domain migrations from PostfixAdmin and Plesk to an existing Mailcow installation.

**Status: pre-alpha (`0.1.0.dev0`).** Configuration, inspection, exports, planning and inactive account staging are implemented.
Target isolation and PostfixAdmin login and delivery controls are available as separate operations.
RoundCube source snapshots, SOGo proposals, session previews and guarded session revocation are also available.
Full source isolation, message transfer, activation and full cutover remain pending.
This snapshot has not been validated end to end.

| Command | Available functionality |
| --- | --- |
| `configure` | Interactive setup for one or both sources and the Mailcow target. |
| `check-config` | Local configuration validation. |
| `inspect` | Source inventories, Maildir checks and authoritative DNS checks. |
| `export` | Private account/configuration snapshots with completion manifests and file hashes. |
| `plan` | Account, source webmail and shared server assessments, quota calculations, policy decisions and initial target conflict checks. |
| `accounts` | Preview account operations, or stage inactive accounts with explicit `--apply`; retain resumable operation receipts. |
| `verify` | Read back staged domain, mailbox, alias, BCC and recovery settings against the saved account plan. |
| `target-isolation` | Preview, pause and check Mailcow access, delivery and writers for a staged batch; retain guarded restoration state. |
| `source-logins` | Preview, freeze, check and release PostfixAdmin batch logins, with source-side configuration backups and a private journal. |
| `source-delivery` | Preview, pause and check PostfixAdmin delivery, recipient expansion and RoundCube sessions, with recovery records. |
| `roundcube-sessions` | Save/review source snapshots and SOGo proposals; revoke/check batch sessions after source isolation. |

The minimum interpreter is Python 3.8. The application uses the Python standard library, with bundled source-server helpers
and external mail/transfer commands where required. Planned work includes server orchestration, conversion, verification and recovery.

The initial deployment model runs on the Linux Mailcow host with root privileges and SSH access to the selected source.
Each migration covers one primary mail domain and its mail alias domains. Source adapters share the target implementation
and migration state model. Runtime helpers are included in this package.

## Installation

From this project directory on the Linux Mailcow host, using a Python 3.8 or newer environment with pip:

```bash
python3 -m pip install .
mailcow-migrate --version
```

There are no third-party runtime packages. Installation uses setuptools; the Python 3.8 build environment selects a
compatible setuptools version below 75.4. See the [setuptools version history](https://setuptools.pypa.io/en/latest/history.html#v75-4-0).

## Configuration workflow

```bash
mailcow-migrate configure --config migration.json
mailcow-migrate check-config --config migration.json
```

The wizard first asks whether to configure PostfixAdmin, Plesk, or both. It collects only the selected source connections,
the Mailcow installation/API, credential-file paths, optional SSH and
RoundCube paths, quota/policy settings, and default quarantine notification frequency/category. Hosts and the API origin
are entered explicitly. Defaults are displayed; Enter accepts a default and `q` cancels. The optional PostfixAdmin RoundCube path can be left
unset for the later webmail workflow to resolve.

The resulting versioned JSON is written atomically with mode 600. An existing file is never overwritten by the wizard.
Credential files are referenced by path; the wizard does not create or inspect them. MySQL credentials refer to a file on
the PostfixAdmin source, while API-key, SSH-configuration, and policy paths refer to the Mailcow host.

See [the PostfixAdmin-only example](examples/migration.example.json) for the file format. Omit an unused source section
or set it to `null`; at least one source must be configured. Existing configurations containing both remain valid.
Only configured sources are candidates for inspection. Edit an existing configuration and
run `check-config` to validate its structure and values. Validation does not contact servers or establish dependency,
credential, or migration readiness. Server-side state is not changed by either command.

All available workflows consume this configuration through the shared loader.

Quarantine defaults live in `migration.quarantine_notification` (`never`, `hourly`, `daily`, `weekly`) and
`migration.quarantine_category` (`add_header` for Junk folder, `reject` for rejected messages, `all` for both).
The wizard initially proposes `weekly` and `add_header`; older configuration files without these fields use the same defaults.
The shared `MigrationConfiguration.quarantine_attributes` resolver preserves available normalized source values and uses
the configured default independently for each missing or null value. Invalid source values are rejected.
Both source planners use this resolver when constructing mailbox payloads, which account staging applies and verification checks.

## Source inspection

After configuring the selected sources, run on the Linux Mailcow host:

```bash
mailcow-migrate inspect example.org --config migration.json
mailcow-migrate inspect example.org --config migration.json --output inspection-example.org.json
```

Inspection uses `ssh` and `dig` locally. It queries only configured sources using the existing source credentials, checks
primary/alias domains, mail-service status, mailbox names, Maildir directories and host addresses, then compares authoritative
MX and A/AAAA records across nameservers. An unconfigured source is never contacted. Configured sources must all be reachable;
a failed query stops inspection rather than treating that source as absent. Source SQL and filesystem checks require the
capabilities listed in [compatibility](docs/compatibility.md).

The command reports ambiguous DNS matches, inactive/missing domains, missing Maildirs, alias-domain inputs, and mail aliases
that route elsewhere. It requires explicit MX records and rejects CNAME mail hosts and disagreeing authoritative answers.
Domains containing aliases but no mailboxes can be inspected. Exit status is 0 when the inspected source and DNS evidence
agree, 1 for findings or operational failures, and 2 for invalid configuration or domain input.

The optional JSON report contains source inventories and routing evidence and is written with mode 600. Existing files are
never overwritten. Mail services are not changed. This is current-source inspection: it does not load migration journals,
recognize a previously activated Mailcow target, validate resume, inspect target readiness, or produce a migration plan.
DNS already pointing at Mailcow may therefore produce a finding. A successful inspection is not migration approval.

## Source export

After updating the installed package, run on the Linux Mailcow host:

```bash
mailcow-migrate export example.org --config migration.json
```

Export repeats live inspection, requires one matching configured source with no inspection findings, and holds the shared
operation lock. It automatically creates `/root/mailcow-in-progress/exports/DOMAIN/EXPORT_ID/` using the domain and a generated
unique export ID, and prints the location. Existing exports are retained. It saves the inspection report and source connection identity before
collecting account/configuration data, then rechecks the selected source domain and Maildir inventory before completion.

| Source | Preserved export evidence |
| --- | --- |
| PostfixAdmin | SQL schema and rows, Postfix/Dovecot versions and effective configuration, queue inventory, configuration archive, maps, default and personal Sieve. |
| Plesk | Accounts and credentials, aliases, forwarding, quota and recovery settings, personal Sieve, domain parameters, autoresponder/list records and source-reported blockers. |

PostfixAdmin uses the bundled Bash/AWK exporter and local `scp -O` with the configured SSH options. It creates private
staging directories on the source under `/root/mailcow-in-progress/scripts` and `/root/mailcow-in-progress/exports`; their
paths are recorded in the manifest and retained after success or failure. Plesk streams its JSON snapshot over SSH and
uses one bundled Python helper shared with inspection. All runtime helpers are included in the package.

The output contains `inspection.json`, `manifest.json`, and a `data` directory. PostfixAdmin data retains its existing
export layout; Plesk data is stored in `data/source.json`. The manifest records source identity, timestamps, source-reported
blockers, and file sizes/SHA-256 hashes relative to `data`. The manifest starts with `complete: false` and is replaced
atomically with `complete: true` only after validation and the inventory recheck. A missing manifest or a false completion
flag means the export is incomplete. Interrupted/failed exports are retained; rerunning the command creates a new export automatically.
This command does not resume or alter migration batches.

Local directories use mode 700 and files use mode 600. Exports contain passwords or password hashes and server credentials;
the PostfixAdmin dump includes the whole set of configuration tables to preserve cross-domain dependencies. Completed command
diagnostics and exporter progress are saved in private diagnostic files when present, with their paths reported in the terminal.

Export completion means the evidence was collected, not that it can be migrated. Plesk blockers are retained and counted.
Account mapping, quota calculation and initial target checks are available through `plan`; full migration assessment remains pending.
The inventory recheck does not establish that every source setting stayed unchanged throughout the export.
Accounts, messages, mail-service configuration and Mailcow are
not modified. RoundCube snapshots are collected separately by `roundcube-sessions`; shared-server preparation and full migration resume remain pending.

## Account planning

Run as root on the Linux Mailcow host, using a completed Python export and a new output directory:

```bash
mailcow-migrate plan example.org --config migration.json \
  --export /root/mailcow-in-progress/exports/example.org/EXPORT_ID \
  --out /root/mailcow-in-progress/plans/example.org/first
```

Planning verifies the export's domain, configured source connection, completion marker, file sizes and SHA-256 hashes.
It parses SQL and maps PostfixAdmin/Plesk accounts, including password schemes, source activity,
static alias precedence, null delivery, catch-alls, alias/forwarding cycles, alias-domain chains, BCC and source blockers.
Quarantine defaults are resolved per mailbox from the configuration. Hashes and any source credentials remain in private files.

By default it checks source inventory, measures Maildirs over SSH using the bundled sizing script, then rechecks inventory.
Quota calculation preserves source finite limits, configured headroom/minimums and conservative CRLF sizing, including compressed
messages with supported metadata. Unknown compression without usable metadata stops sizing. Mapping blockers skip sizing and mark
quotas provisional. PostfixAdmin also accepts the `--quota-mib N` override, which skips source contact and measurement;
the configured headroom/minimums are not used in that mode. Target checks still run, while source transfer checks remain pending.
Plesk always uses measured quotas.

Read-only target checks cover local prerequisites, Docker Compose, running Dovecot/Postfix services,
`MAILDIR_SUB=Maildir`, and API domain/mailbox/alias/BCC inventories. An existing primary domain is reusable only when empty,
with no relevant alias-domain, alias or BCC dependencies. Its proposed setting changes are recorded. Conflicting existing
objects remain blocked; an earlier batch must be recovered using its original plan and journal, never adopted by name alone.

Storage inspection resolves the Dovecot container's `/var/vmail` mount and reads the `vmail` UID/GID used for copying.
Mailbox destinations must stay below that resolved volume and have safe directory ancestors. For batches containing mailboxes,
the receiver's rsync options are checked independently of the source sender options; ownership and progress options are not
required on the sender. Normal measured planning also verifies current source Maildirs and binds their location to the source
used for quota calculation. Fixed-quota planning and plans with source mapping blockers defer source transfer checks.

For existing target Maildirs, a checksum dry run checks append compatibility with the transfer exclusions for Dovecot indexes,
`maildirsize` and temporary messages. Different existing contents or file types, symlinks, unsupported paths and message filename
IDs found under another folder or flag name are blockers. Dovecot UID/keyword metadata conflicts require a mailbox-aware merge.
Missing target directories are reported for later creation. No mailbox directories or messages are created, replaced or removed.
These comparisons can take time on populated mailboxes; inventories are kept in private temporary files and removed after inspection.

The configured policy supplies administrator names and batch-only ALL grants, explicitly reviewed administrator merges,
sender permissions, and the source password-expiration decision. Recovery email/phone, fetchmail, inactive Sieve, and mailbox
forwarding checks report unsupported mappings. Plesk forwarding retains its keep-copy choice for later personal Sieve import.
No account, mail-service or policy changes are applied. Sizing uses a temporary source file listing that its helper removes.

Source webmail assessment reads personal/default Sieve and maps the supported PostfixAdmin subset to editable SOGo filter
definitions. Unsupported conditions/actions, string lists, folder hierarchies, modified UTF-7 folder names, external includes,
and inactive scripts remain blockers. An empty personal script stays empty; only a missing personal script selects the default.
Plesk personal Sieve is preserved for copying, with forwarding generated according to its keep-copy setting; it is not subjected
to the PostfixAdmin editable-filter conversion. Plesk autoresponders and forwarding combined with personal Sieve remain blocked.

PostfixAdmin vacations are assessed even when inactive or expired. UTC provenance, the approved 1–30 day reply interval,
whole-day boundaries in `vacationTimeZone`, supported timestamp range and safe text representation must all be preserved.
An interval of zero requires an explicit `vacationZeroIntervalDays` policy decision. Native SOGo preferences and inactive
staging prefilters are proposed privately. Forwarding stays in a Mailcow prefilter when needed to let personal filters
control the local copy; otherwise it maps to SOGo Forward settings. No source rule is rewritten or target setting applied.

Shared PostfixAdmin settings are assessed from the exported Postfix/Dovecot configuration and complete `transport`,
`sender_access` and `rbl_override` maps. The report classifies supported mappings, informational settings, unsupported settings
and blockers, including rules shared with other domains. Destination-specific routes and the default outgoing relay are
separate proposals. Missing referenced maps, unsupported lookup chains, custom transport options, canonical rewriting and
custom delivery filters retain their blocking behavior. Temporary migration hold overlays are normalized for classification;
planning does not verify or change those holds.

An individual-IP `OK` rule can be proposed as a Postscreen whitelist adaptation. This bypasses Postscreen checks for that IP;
Rspamd filtering and SMTP relay/recipient restrictions remain in place. Exact sender rejection rules and supported SMTP routes
have separate mappings. Unsupported policies and server administrator identities remain recorded for operator review.

Sender-permission assessment inspects each SMTP service, configuration references, service overrides, restriction ordering,
cleanup services and external filters. It distinguishes disabled authentication, discovered ownership rules, absent ownership
checks and unresolved evidence. It never derives broad send-as grants from those findings; `senderPermissions` still requires
explicit batch users and addresses. Plesk planning does not import PostfixAdmin shared settings or assume missing sender evidence.

The new directory contains `plan.json`, `account-policy.json`, `webmail-plan.json`, `shared-settings-plan.json`, `transfer-plan.json`, `policy.json`,
`assessment.json`, `summary.txt`, `users.txt`, `domains.json`, measurement diagnostics when present, and a completion manifest.
Directories use mode 700 and files mode 600. Full shared-setting values and per-service details stay in the private report.
Its `automatic` classification means a supported mapping is known; no change has been selected, applied or verified.
The shared operation lock covers planning. Existing outputs are never overwritten; interrupted runs retain an incomplete manifest.
Exit status 1 reports blockers or unresolved policy decisions; 0 means planning completed without those findings.

**A completed plan is not migration approval.** `assessment.json` always records `migration_ready: false` in this version.
`webmail-plan.json` records proposed source mappings and `target_verified: false`. Target Sieve compilation, SOGo capability
and conflict checks, container timezone agreement and actual import verification remain pending. The shared-settings report
records `target_verified: false` and `selection_recorded: false`: selection/exclusion review, relay credentials, server administrator
privileges, shared-setting application and target verification remain pending. `transfer-plan.json` records the volume, vmail identity,
capability checks and mailbox preview with `transfer_ready: false`. It does not verify account/transfer journals, stopped writers,
activation state or available disk capacity. Paths and append compatibility must be rechecked under isolation before copying.
RoundCube target assessment/import, transfer execution, full source isolation, DNS handoff and full migration resume remain pending.
Source inventory rechecks do not prove all account settings stayed unchanged after export. Plans record a digest for
their assessment. Planning does not resume an existing migration batch; retain its saved state and recovery evidence.

## Account staging and verification

Use the unchanged plan and configuration from a completed planning run:

```bash
mailcow-migrate accounts --config migration.json --plan /root/mailcow-in-progress/plans/example.org/first/plan.json
mailcow-migrate accounts --config migration.json --plan /root/mailcow-in-progress/plans/example.org/first/plan.json --apply
mailcow-migrate verify --config migration.json --plan /root/mailcow-in-progress/plans/example.org/first/plan.json
```

`accounts` previews by default. Only `--apply` writes to the Mailcow API. It stages mailboxes with their planned passwords,
quotas, names and quarantine settings; imports and checks recovery email addresses; adds inactive alias domains, separate
aliases/catch-alls and BCC rules; then disables the primary domain. Native mailbox delivery routes are left to mailbox creation.
The domain is temporarily enabled for account creation and disabled on successful completion. An interruption retains earlier
changes and the journal; it does not automatically undo them. Domain administrators, sender grants, filters and shared settings
are separate workflows and are not applied by this command.

The command validates the planning manifest, plan fingerprint, saved configuration and account mappings. Applying also requires
the saved assessment to contain no blocked findings or unresolved decisions. Live target conflicts are checked before the first
account operation. An existing primary domain is reused only when empty and free of relevant alias/BCC dependencies, with its
original settings saved before editing. Existing objects are never adopted as a previous batch merely because their names match.
Conflict details are saved privately in `target-conflict.json`.

`state.json` binds account operations to the plan fingerprint, API origin and local Mailcow directory. The shared operation lock
covers preview, staging, reconciliation and verification. Each request is recorded before it is sent; successful operations receive
receipts and are skipped on later runs. Changing the plan or target stops reuse of the journal. Retain the same batch directory
to resume account staging, and never delete its journal to bypass a conflict. Staging refuses a recorded activation in progress.

An uncertain request is never blindly repeated. On a later invocation, read-only inventory checks must establish every requested
value. Password fields require a read-only hash check inside `php-fpm-mailcow`; credentials travel through standard input and
are not printed. A missing, ambiguous or mismatched target object leaves the request pending and stops further writes. A confirmed
request is recorded as completed without another API write. Thus a preview or verification can reconcile the local journal while
leaving target accounts unchanged. Password-check diagnostics, when present, are saved privately in the batch directory.

`verify` requires completed staging receipts and compares domain limits, account activity, mailbox quotas/names/quarantine,
aliases, alias domains, BCC and recovery emails with the saved plan. It does not test password login, messages, filters,
sender permissions or delivery. Full source isolation, message copying, activation and full migration recovery remain pending;
these commands provide the account-staging step, not a complete migration workflow. Only this application's planning and
account-journal formats are accepted. Keep other operation journals with the tool that created them.

## RoundCube snapshots and sessions

Preview webmail data and session ownership before freezing source logins:

```bash
mailcow-migrate roundcube-sessions --config migration.json --plan /root/mailcow-in-progress/plans/example.org/first/plan.json
```

The installation path comes from the selected source configuration. If it was left unset, add `--directory /path/on/source`
to this first preview. The chosen path is then pinned to the batch; subsequent commands reuse it without changing the saved
configuration. The source needs RoundCube 1.6.x, PHP CLI 7.3+ with `pdo_mysql`, supported MySQL configuration and InnoDB user-data
tables. The exporter runs as a private temporary PHP file over SSH and removes that file afterward. Unsupported configurations,
plugins, address-book storage, session storage or session ownership stop the operation.

Preview leaves source data and sessions unchanged. It saves a consistent snapshot of selected profiles, contacts, identities,
preferences and other persistent records, plus ignored-profile metadata and session counts. For duplicate profiles it selects
the latest login, breaking ties by highest user ID; profiles without a batch mailbox are reported and left on the source.
Stored files are counted and fingerprinted, not downloaded. Unknown tables, shared dictionary data, groups, saved responses,
dictionaries, searches and stored files retain their preservation blockers. The same preview now builds SOGo proposals for
supported contacts, collected recipients, identities, preferences and folder display state. Target checks/import remain pending.

Each snapshot produces an immutable `proposal-HASH.json` and matching text review under the private `roundcube-sessions`
directory. Stable contact/book identifiers preserve distinct source records; existing vCard UIDs are retained, while missing
UIDs receive deterministic values. Duplicate UIDs, linked card assets, unsupported vCards and trusted-sender image permissions
are blockers. Collected recipients retain their names, email addresses and raw timestamps as separate labelled contacts.

Preference proposals preserve the supported locale/date/time, compose, threading, reading-delay, autosave and signature mappings.
Automatic/missing timezone uses Europe/Rome, blank identity names use their email address, and a missing default identity selects
the mailbox identity or creates one. Different sender addresses require verified send-as ownership; unsupported identity fields,
external signature assets, conflicting signature formats and unknown preferences stop progress. Interface preferences deliberately
left to SOGo's native behavior are listed with their original values retained in the source snapshot.

When `collapsed_folders` is present, preview reads the source Dovecot namespace and folder tree, validates encoded folder names,
and records the intended collapsed paths. Missing folders or unsupported hierarchy/layout block the proposal. Source folder trees
are rechecked with the user-data snapshot before isolation continues. Final target expansion settings require copied mail and
target-folder verification; the source proposal does not claim that work is complete.

Read both proposal files before repeating preview with `--reviewed`. This records review only when the proposal exactly matches
one already saved by a previous invocation. A new or changed proposal is saved for review, but cannot be approved in that same
invocation; existing approvals do not carry across changed source data or folder trees. Source isolation requires the matching
review record and no hard blockers. A successful preview alone is not migration approval.

Only automatic read receipts (`mdn_default`), delivery notifications (`dsn_default`) and reply-folder placement
(`reply_same_folder`) can be explicitly deferred. Their source values and proposed fallback behavior appear in the private review.
If they are enabled, use `--reviewed --accept-deferrals` after inspecting that unchanged proposal. This preserves the values for
later work; it does not import or silently discard them. Feature references in the proposal do not establish support in the
installed target, and deferred preferences must be revisited through a verified mapping before any later application.

Ask every migrating user to recover browser-only drafts in every browser/profile, save them in server Drafts, verify they
reopen, and close the clients. Repeat preview with `--drafts-saved` to record that confirmation, alongside `--reviewed` after
reviewing the unchanged proposal. The script cannot inspect
browser local storage. Source login freeze and delivery pause require this confirmation and an unchanged source snapshot;
keep user edits and account administration frozen afterward. Login release remains available when these preflights fail.

The PostfixAdmin sequence is RoundCube proposal review/deferral and draft confirmation, source login freeze, then source delivery pause.
`source-delivery --action pause --apply` installs and verifies delivery holds before revoking RoundCube sessions. It includes
all profiles belonging to batch mailboxes, including duplicate and alias-domain logins, while retaining unrelated sessions.
The helper matches decoded ownership and deletes each session only if both its ID and stored serialized value still match;
changed sessions stop the operation. It then checks for zero remaining batch sessions and rereads the persistent snapshot.

`roundcube-sessions --action revoke --apply` can resume this session step separately after the same live login and delivery
checks. `--action check` verifies completed revocation without changing sessions. Plesk currently supports preview only;
revocation requires its pending source-isolation adapter. No operation changes contacts, identities or preferences on the source,
imports data into SOGo, copies mail or activates accounts.

Private evidence lives under `roundcube-sessions` in the batch directory. Its journal records the source, target, plan,
installation, baseline snapshot, proposal review/deferrals, draft confirmation and each attempt's before/removed/after counts. Snapshots are retained
by fingerprint, including changed snapshots found during verification. An interrupted revocation may already have committed;
resume against the same baseline after restoring the required isolation. Preview cannot replace an unfinished operation's
baseline, and persistent-data drift requires reconciliation. Existing journals without proposals can add the new review metadata
through preview while retaining their session receipts and unchanged baseline. Invalid exporter output is retained privately for diagnosis.

Session completion is distinct from webmail-import completion: this journal retains `import_complete: false` and
`source_isolated: false`, and cannot satisfy Mailcow's webmail-import release guard. Proposals retain `target_verified: false` and
`import_complete: false`. Target configuration/conflict checks, actual SOGo import/readback, combined submission
checks and migration readiness remain pending.

## PostfixAdmin source logins

Preview the login-control step using the saved plan and configuration:

```bash
mailcow-migrate source-logins --config migration.json --plan /root/mailcow-in-progress/plans/example.org/first/plan.json
```

The default `preview` action checks the configured source, required commands, effective Dovecot passdb and supported SQL query.
It identifies whether the exact batch exclusion already exists and rejects exclusions that partially overlap these users.
Only a single SQL passdb using `/etc/dovecot/dovecot-mysql.conf` is supported. The base password query must match the supported
form; existing migration exclusions for other batches are retained. Wildcard mailbox names are rejected before remote commands.
The plan's source connection must match its planning manifest and current configuration. Plesk requires its separate adapter,
which remains pending; an alias-only batch with no mailbox logins needs no login operation.

After users have saved drafts and closed clients, `--action freeze --apply` excludes exactly the planned mailbox usernames,
reloads Dovecot, clears their authentication caches and disconnects their sessions. Before changing logins, it checks the
delivery prerequisites described below and the saved RoundCube snapshot/session preview with draft confirmation.
These blockers stop freeze, while login release remains available.
Each account must produce the expected
absent-entry authentication result, and session checks must show no remaining connections. Unexpected command failures stop
the operation; a shared Dovecot process is never forcibly killed to disconnect one batch. Passwords and mailbox activity flags
remain unchanged. Saved blocked findings and unresolved decisions prevent a freeze.

Before replacing the query, the source helper locks its configuration operation, checks the full file hash, and saves a private
backup under `/root/mailcow-in-progress/auth-backups`. It preserves ownership, permissions and attributes, verifies the backup,
and checks for drift again before replacement. SQL connection credentials and full configuration backups stay on the source.
The private local `source-logins.json` journal records source/plan/user identity, the query change, backup location, verification
hashes and the stage being attempted. Keep this journal and the source backups after any interruption; an earlier change may
already be installed. Repeating freeze rereads the supported live query and finishes reload/cache/session work without adding
another copy of the same exclusion. `--action check` verifies a completed freeze without changing source configuration or sessions.
Configuration drift requires a new preview before continuing with a reviewed freeze.

`--action release --apply` removes only the exact batch exclusion, reloads Dovecot and clears those users' caches, then stops.
It remains available when unrelated saved planning findings block freeze. Freeze and release both refuse recorded target
activation or an uncertain target write. Release leaves separate delivery holds and Mailcow staging in place and moves no mail;
review those holds before returning users to the source. Do not restore an entire SQL configuration over later batches' changes.

This command implements login control only. Delivery controls and RoundCube session revocation are separate steps.
Complete submission checks and the combined migration workflow remain pending; a successful login freeze does not establish
full source isolation or migration readiness.

## PostfixAdmin source delivery

Preview delivery controls using the same saved plan and configuration:

```bash
mailcow-migrate source-delivery --config migration.json --plan /root/mailcow-in-progress/plans/example.org/first/plan.json
```

The default `preview` action reads source routing, BCC maps, configuration ownership and directly referenced mail-sync cron jobs.
It also requires and rechecks the saved RoundCube snapshot and session preview.
It requires one primary domain, supported hash BCC maps, the retry/error transport, regexp maps, and an empty or `+` recipient
delimiter. Linked domains, conflicting routes, service-specific overrides, global `always_bcc`, changed managed files and
unsupported map types stop the operation. The same routing preflight now runs before a source login freeze.

`--action pause --apply` requires the saved login freeze to pass current configuration, authentication and session checks.
An alias-only batch has no login requirement. Saved blocked findings, unresolved decisions, recorded target activation and
uncertain target writes prevent the pause. Under the shared lock, the command records its intent, backs up source configuration,
adds domain and exact-mailbox retry routes, compiles maps, reloads Postfix and drains old delivery workers.

The next stage preserves accepted recipient addresses and catch-all behavior through an identity alias map, preventing this
domain's source alias/vacation expansion. It uses filtered copies of supported BCC maps, preserving originals and other domains'
entries. Delivery stays deferred while mappings are installed, workers drain and queued envelopes are checked. Mailbox and
domain route lookups, accepted/unknown-recipient behavior, detected scheduled jobs and open writable mailbox files must verify.
The command then revokes batch RoundCube sessions and verifies their absence and the unchanged persistent user-data snapshot.
These checks cannot discover every writer hidden behind a custom script; source account administration and custom batch imports
must remain paused separately.

The source records queue IDs present after recipient isolation. Queued recipients with prior alias/vacation expansion,
unaccounted original-recipient history, ambiguous BCC copies or administrative holds require reconciliation. The global
original-recipient tracking setting is preserved. Postfix versions supporting envelope-only `postcat` use it; the alternate
format is parsed locally and message headers, bodies and attachments are discarded rather than returned or logged.
The delivery command never deletes messages, requeues mail, flushes the whole queue or starts forwarding to Mailcow.
Its preview cannot establish queue safety: the final queue checks happen after recipient isolation and worker draining.

The private local `source-delivery.json` journal binds operation stages and backup locations to the source, API origin and plan.
Source configuration backups remain under `/root/mailcow-in-progress/delivery-backups`; source routing and queue-history records
are retained with the managed maps. File/configuration fingerprints, source locks and readback protect each update. An interrupted
pause can leave holds or mappings installed; retain all records and rerun the same command after resolving the reported blocker.
`--action check` verifies a completed delivery pause, its login freeze and the separate RoundCube session receipt without
changing services or queue scheduling.

This snapshot provides no delivery-release command. Holds remain until guarded handoff is implemented and completed; releasing
source logins alone does not remove them. The delivery journal explicitly records `source_isolated: false` and
`webmail_sessions_verified: false`; session verification is recorded separately in the RoundCube session journal.
Webmail import, complete submission checks and combined source readiness remain pending, so this command does not authorize
copying, activation or cutover. Plesk retains its separate pending adapter.

## Target isolation

After account staging, preview Mailcow isolation using the same saved plan and configuration:

```bash
mailcow-migrate target-isolation --config migration.json --plan /root/mailcow-in-progress/plans/example.org/first/plan.json
```

The default `preview` action reads inventories and prerequisites without changing target services or the isolation journal.
It checks protocol controls, sync-job ownership, SOGo port exposure, Dovecot replication, nginx include support and delivery-map
compatibility. Source isolation and the absence of remaining target writers are not established by this preview.

`--action pause --apply` saves original protocol controls and sync-job activity, defers batch delivery through a persistent
Postfix transport-map hold, and blocks batch SOGo resource paths through nginx. It drains old delivery/web workers, pauses
batch sync jobs, terminates their identified imapsync processes, disables mailbox protocol access, clears authentication caches
and disconnects sessions. Other domains' transport-map entries and saved protocol/job settings are retained.

Completion requires readback of disabled access and jobs, no remaining batch sessions or imapsync workers, HTTP 503 responses
from the SOGo guard, intact delivery guards, and no detected scheduled/open-file writers. Scheduled writers referring to the
batch and jobs with unknown ownership stop the operation; shared jobs are not automatically disabled. A failure retains any
changes already made and the recovery journal. Resume with the same pause command; `--action check` rechecks a completed pause
without changing target services. These checks do not certify source isolation or discover every possible external writer.

The private `target-isolation.json` journal binds backups and operation state to the plan, API origin and Mailcow directory.
Configuration changes are recorded before atomic replacement while retaining existing owner/mode; interrupted route updates
are reconciled only when the live file and effective route still match the saved before/after values. Shared Postfix snapshots
are retained separately. Changed or unowned configuration blocks continuation. Keep the journal and backups with the batch.

`--action release --apply` restores saved access, delivery and sync-job settings only after the matching account journal records
completed activation and the webmail journal records completed import. The activation and webmail-import workflows are still
pending in this pre-alpha, so a normally staged batch cannot yet pass the release guard. A pause therefore leaves the batch
isolated until those workflows are available and completed; this command is not a complete pause/cutover/restore workflow.
Initial isolation refuses activation in progress, a released batch or an interrupted release. Repairing webmail on an already
activated batch and full source isolation remain separate planned work. Isolation never copies mail or activates accounts.

## Package layout

| Module | Responsibility |
| --- | --- |
| `cli`, `wizard`, `configuration` | Entry point, interactive setup, immutable settings, and shared validation. |
| `inspection`, `core.dns`, `sources.inventory` | Read-only source inventories, authoritative DNS agreement, and inspection reports. |
| `exporting`, `sources.export` | Private source exports, inventory rechecks, completion manifests and file hashes. |
| `planning`, `plans`, `sources.sql`, `sources.usage` | Account mapping, policy decisions, export validation and measured quotas. |
| `accounts`, `plans.saved`, `targets.account_inventory`, `targets.reconciliation` | Saved-plan validation, inactive account staging, receipts and readback. |
| `isolation`, `targets.isolation`, `targets.writers` | Target access/delivery isolation, writer checks, recovery journals and guarded restoration. |
| `source_logins`, `sources.logins` | PostfixAdmin login-query exclusions, source configuration backups, session checks and guarded release. |
| `source_delivery`, `sources.delivery`, `sources.php` | PostfixAdmin delivery holds, recipient identity routing, BCC filtering and queue safeguards. |
| `roundcube_sessions`, `sources.roundcube` | Source webmail snapshots, session ownership, draft confirmation and revocation receipts. |
| `plans.roundcube`, `plans.roundcube_contacts`, `plans.roundcube_preferences`, `core.folders` | Reviewed SOGo proposals, source folder evidence and preserved blockers. |
| `plans.sieve`, `plans.native_settings`, `plans.webmail` | Source Sieve conversion, vacation/forwarding proposals and webmail assessment. |
| `plans.postfix`, `plans.global_settings`, `plans.senders` | Shared source settings, routing relationships and per-service sender-policy assessment. |
| `targets.readiness` | Read-only target prerequisite checks, account conflicts and empty-domain reuse assessment. |
| `targets.storage`, `plans.transfer`, `sources.maildirs` | Mail volume and identity discovery, source paths, rsync capabilities and append previews. |
| `core.commands`, `core.ssh` | Command execution, timeouts, private diagnostics, and SSH/SCP/rsync arguments. |
| `core.storage`, `core.state` | Private JSON files, a shared operation lock, identity checks, and mutation journals. |
| `sources` | Source connections, inspection and domain account/configuration export adapters. |
| `targets.mailcow` | HTTPS API requests, safe read retries, and Docker Compose commands. |
| `resources` | Packaged source inspection/export helpers and the existing policy example. |

CLI errors use exit status 1, configuration/usage errors 2, interruption 130, and shared-lock contention 75.
Library callers must hold the workspace lock and verify batch identity before journaled writes. Pending writes require
reconciliation before proceeding. Account staging can resume its own recorded operations; full migration recovery remains planned work.
The API client verifies TLS, rejects redirects, and never retries writes automatically. Command diagnostics are private
by default; callers may display known-safe output with explicit secret redaction.

- [Compatibility and dependencies](docs/compatibility.md) defines version boundaries and required capabilities.
- [Feature roadmap](docs/features.md) tracks implemented capabilities, planned work, configurable decisions and blockers.
