# Mailcow Migration

A Python CLI for preparing mail-domain migrations from PostfixAdmin and Plesk to an existing Mailcow installation.

**Status: pre-alpha (`0.1.0.dev0`).** Configuration, inspection, exports, planning and inactive account staging are implemented.
Source selection checks saved batch ownership and can recognize recorded Mailcow addresses after activation and DNS handoff.
Target isolation and PostfixAdmin login and delivery controls are available as separate operations.
RoundCube source snapshots, SOGo proposals, session previews and guarded session revocation are also available.
SOGo target preparation can check/configure custom identity support, inspect existing user-data conflicts and assess target folder display state.
An explicit user-preparation action can refresh missing SOGo user sources and reconcile cached unknown-user lookups.
Append-only mail copying is available for both sources with isolation checks and explicit stopped-writer confirmation.
Plesk supports journaled source isolation, personal Sieve/forwarding installation, guarded activation and quota recalculation.
Guarded personal/default Sieve installation and compilation are available separately.
Reviewed forwarding/vacation prefilters can be staged inactive with API readback and uncertain-request reconciliation.
Explicit sender-permission mappings can be merged into inactive mailboxes while retaining existing grants and security requirements.
Reviewed domain administrators can be imported after activation, preserving existing memberships/security and generating private new credentials.
Guarded webmail import connects RoundCube data, editable SOGo filters and native vacation/forwarding settings with native readback.
Guarded PostfixAdmin account activation includes copied-Sieve checks, staged prefilters and preservation of completed native handoffs.
Quota recalculation is available for activated mailboxes under isolation. Source submission checks, forwarding, certificate/signing
setup and confirmed handoff are available as explicit operations and through the guided domain workflow.
This snapshot has not been validated end to end.

| Command | Available functionality |
| --- | --- |
| `configure` | Interactive setup for one or both sources and the Mailcow target. |
| `check-config` | Local configuration validation. |
| `prepare` | Check configured sources and Mailcow; `--guided` reviews private credential setup and shared server settings. |
| `migrate` | Guide a domain through planning, isolation, copying, activation, webmail and confirmed handoff; resume its original batch. |
| `assess` | Save a consolidated read-only assessment for the before, staged, activated or handoff phase. |
| `inspect` | Source inventories, Maildir checks and authoritative DNS checks. |
| `select-source` | Select the source using live evidence and saved journals; recognize recorded target addresses after activation. |
| `export` | Private account/configuration snapshots with completion manifests and file hashes. |
| `export-server` | Private shared PostfixAdmin configuration/schema and server-administrator export. |
| `server-settings` | Review selections/exclusions, then apply or verify shared routes, relay credentials, sender rules and full-server administrators. |
| `plan` | Account, source webmail and shared server assessments, quota calculations, policy decisions and initial target conflict checks. |
| `accounts` | Preview account operations, or stage inactive accounts with explicit `--apply`; retain resumable operation receipts. |
| `verify` | Read back account/recovery inventories; optionally compare stored mailbox hashes with the plan using `--passwords`. |
| `target-isolation` | Preview, pause, check and restore Mailcow access/delivery; support separately journaled webmail repair after activation. |
| `source-logins` | Preview, freeze, check and release PostfixAdmin batch logins, with source-side configuration backups and a private journal. |
| `source-delivery` | Preview, pause and check PostfixAdmin delivery, recipient expansion and RoundCube sessions, with recovery records. |
| `source-isolation` | Preview, freeze, check and release Plesk login/submission/delivery controls before activation. |
| `source-forward` | Preview or start verified per-domain delivery to Mailcow after import and target restoration. |
| `certificate` | Preview, request or verify client-hostname certificates, preserving other names and recorded backups. |
| `signing` | Review Mailcow/provider DKIM and apply only missing-key generation or the selected domain relay assignment. |
| `handoff` | Verify TLS, DNS, signing, source forwarding, mailbox readability and queues; record completion after explicit confirmations. |
| `roundcube-sessions` | Save/review source snapshots and SOGo proposals; revoke/check batch sessions after source isolation. |
| `sogo-target` | Prepare identity settings/user lookups, check effective settings, and inspect native data/folder-state conflicts. |
| `mail` | Preview or append Maildir files under isolation, preserving existing files and recording applied/final passes. |
| `sieve` | Preview or install exported personal/default filters, with explicit conflict replacement, backups and target compilation. |
| `prefilters` | Preview or stage inactive forwarding/vacation prefilters from the reviewed webmail plan. |
| `senders` | Preview, merge or verify reviewed sender permissions, retaining existing grants and mailbox security settings. |
| `admins` | Preview, import or verify mapped domain administrators, explicit membership merges and private new passwords. |
| `activate` | Restore planned account/filter activity after final transfer checks, retaining source and target isolation. |
| `quota` | Recalculate usage for planned active mailboxes after activation, bound to the copied mail volume. |
| `webmail` | Preview or import approved RoundCube data, editable filters and native settings; retain isolation and verified completion receipts. |

The minimum interpreter is Python 3.8. The application uses the Python standard library, with bundled source-server helpers
and external mail/transfer commands where required. Production qualification requires representative staging and recovery runs.

The initial deployment model runs on the Linux Mailcow host with root privileges and SSH access to the selected source.
Each migration covers one primary mail domain and its mail alias domains. Source adapters share the target implementation
and migration state model. Runtime helpers are included in this package.

## Guided operation

```bash
mailcow-migrate prepare --config migration.json
mailcow-migrate prepare --config migration.json --guided
mailcow-migrate migrate example.org --config migration.json
mailcow-migrate assess --config migration.json --plan /root/mailcow-in-progress/plans/example.org/BATCH/plan.json --phase before
```

Run preparation on the Linux Mailcow host as root. The first command checks tools, source database access, Mailcow services
and API access without changing either mail service. The guided variant can create a missing private source MySQL option file,
prompt for a missing API key without echoing it, and review/apply shared PostfixAdmin settings. It contacts only configured sources.
Credential files already present are retained. Shared Postfix changes and server-administrator choices require explicit approval.

The domain guide exports and plans a new batch or resumes its saved plan. Optional `--plan FILE`, `--export DIR` and
`--quota-mib N` reuse existing evidence or select a fixed quota for a new PostfixAdmin plan. Review the private summary and policy;
resolve blocking mappings before proceeding. A changed configuration or policy cannot silently retarget a saved batch.
To choose a corrected plan before mutations, supply its path explicitly; competing operation journals always block a new batch.

The guide prompts before source isolation, target staging, copying, activation, webmail preparation/import, restoration and handoff.
Confirm browser drafts, stopped writers, available disk space/inodes and backup readiness only after checking them.
Default-Sieve use and replacement are explicit choices, initially disabled and pinned when the batch begins.
Custom-identity configuration and missing SOGo user sources may require a shared webmail/DAV restart.
User-source and cached-lookup preparation covers every batch mailbox, including users without a RoundCube profile.
An empty confirmation or `q` pauses with exit code 2 and retains recovery evidence. Interruptions never imply automatic rollback.

Resume the same `migrate DOMAIN --config FILE` command. Once activation starts, the guide skips staging and mail/Sieve copying.
Completed native imports retain later user edits. Incomplete webmail after target restoration uses a separate repair isolation journal.
Before activation, source recovery offers the supported login-only PostfixAdmin release or Plesk access/delivery release, then stops.
DNS changes remain manual; completion requires fresh automated checks and explicit real delivery/client/authentication/permission confirmations.
An uncertain certificate refresh needs the explicit `certificate --action retry --apply` recovery command.

`assess --phase before|staged|activated|handoff` consolidates the applicable findings and saves a private phase report.
Before/staged checks cover identity, source evidence, quota/storage estimates, isolation prerequisites and SOGo capabilities.
Native user-data conflicts are assessed after activation; handoff checks cover live DNS, TLS, signing, queues and receipts.
Use `--hostname NAME` with handoff assessment when the chosen client hostname differs from `mail.DOMAIN`.
Assessment can retain local evidence/backups and reconcile an observed pending receipt; it does not change mail-service settings.
Capacity estimates do not reserve storage, and a successful phase assessment is not approval for later phases or proof of production readiness.

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

Every operational command loads the selected file once through the shared loader before dispatch. Both the installed command
and `python3 -m mailcow_migration` use this entry point. Missing, unreadable, malformed or invalid files stop before server work
with configuration-error status 2 and guidance to correct the file or run `configure`; explicit source hosts and the Mailcow API
origin are required. The `configure` command creates a new file without attempting to load it first. Existing defaults and
saved-plan/journal identity checks continue to apply.

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

## Source selection and batch discovery

Run on the Linux Mailcow host as root before continuing a migration:

```bash
mailcow-migrate select-source example.org --config migration.json
```

Selection checks only configured sources and reuses the inventory, Maildir, authoritative MX/address and alias-domain checks.
It also discovers plans under `/root/mailcow-in-progress/plans/DOMAIN/` and paths recorded in the private `batch-catalog.json`.
Planning registers its output directory before target assessment; commands loading a saved plan register it under the same
shared operation lock. Existing batches outside the discovery tree must be supplied once with `--plan /absolute/path/plan.json`.
Keep all existing batches available; directories outside the tree and catalog cannot be discovered automatically.

The catalog binds each path to its domain and saved configuration. Selection reports incomplete planning and operation journals,
then uses the original plan when exactly one batch has operations. Changed configuration, missing registered files, unsupported
state versions, changed plan fingerprints, competing journals or a different source stop continuation. Released journals remain
ownership evidence. New planning and saved-plan commands also reject another batch's operation journals for that primary domain.
Resolve conflicts while retaining the original records; no journals are converted, removed or treated as permission to overwrite data.

After activation completes and **before changing DNS**, record the Mailcow addresses used for subsequent source selection:

```bash
mailcow-migrate select-source example.org --config migration.json \
  --plan /root/mailcow-in-progress/plans/example.org/first/plan.json --record-target-addresses
```

This reads Mailcow's configured hostname from Compose and its authoritative A/AAAA records, rejecting source-server addresses.
The private `source-selection-target.json`
binds those addresses to the plan, source, API and Mailcow directory. Recording requires completed activation without a pending
account request; repeat recording verifies the first snapshot rather than replacing changed addresses. It changes no mail service.
After DNS changes, selection accepts those saved addresses only with matching activation evidence, applies the same rule to
mail aliases, and still rejects addresses belonging to another configured source. Source configuration and Maildirs must still agree.
Missing or foreign address evidence cannot authorize guessing a source after cutover.

Selection writes private evidence to `/root/mailcow-in-progress/source-detection/DOMAIN.json`. It preserves the batch's settings
and policy files and directs the operator back to the original plan. Selection does not execute pending writes or establish
transfer readiness; target address recording does not certify TLS, SMTP delivery or completion of handoff.

## Source export

After updating the installed package, run on the Linux Mailcow host:

```bash
mailcow-migrate export example.org --config migration.json
```

Export repeats journal-aware source selection, requires one matching configured source with no findings, and holds the shared
operation lock. Existing batches must match their saved configuration and source inventory. Export is blocked after activation
starts or during an uncertain account write. It automatically creates `/root/mailcow-in-progress/exports/DOMAIN/EXPORT_ID/`
using the domain and a generated unique export ID, and prints the location. Existing exports are retained.
It saves the inspection report and source connection identity before
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
This command does not resume pending migration operations; it can register an existing batch in the shared catalog.

Local directories use mode 700 and files use mode 600. Exports contain passwords or password hashes and server credentials;
the PostfixAdmin dump includes the whole set of configuration tables to preserve cross-domain dependencies. Completed command
diagnostics and exporter progress are saved in private diagnostic files when present, with their paths reported in the terminal.

Export completion means the evidence was collected, not that it can be migrated. Plesk blockers are retained and counted.
Account mapping, quota calculation and initial target checks are available through `plan`; `assess` adds phase-specific live findings.
The inventory recheck does not establish that every source setting stayed unchanged throughout the export.
Accounts, messages, mail-service configuration and Mailcow are
not modified. RoundCube snapshots are collected separately by `roundcube-sessions`; `migrate` coordinates the guarded workflow.

## Shared server preparation

For a PostfixAdmin source, complete shared preparation before domain account writes or source freezing:

```bash
mailcow-migrate export-server --config migration.json
mailcow-migrate server-settings --config migration.json --export /root/mailcow-in-progress/exports/server/EXPORT_ID
mailcow-migrate server-settings --config migration.json --export /root/mailcow-in-progress/exports/server/EXPORT_ID --action review
mailcow-migrate server-settings --config migration.json --export /root/mailcow-in-progress/exports/server/EXPORT_ID --action apply --apply
```

Server export retains supported administrator schema/data, effective Postfix/Dovecot settings, maps, queue inventory and a private
configuration archive. It changes no mail service. The default settings preview saves an inventory and selection under the private
`global` workspace directory. Review records individual imports, explicit exclusions and any full-server administrator approvals.
Unsupported required routes and missing evidence block continuation. Excluding an optional setting accepts a behavior difference
and retains the current target configuration; it does not remove an existing rule.

Supported imports are native recipient transports, the default outgoing relay and its archived credentials, exact external sender
rejections, and individual-IP Postscreen exceptions. An IP exception bypasses Postscreen/RBL checks while keeping Rspamd and relay
restrictions. Recipient routes and the default relay are separate choices. Credentials are read from one regular archive member
into memory without extracting archive paths. New administrator passwords are generated privately; existing approved accounts
retain their passwords and security settings. Keep other target configuration and administrator changes paused while applying.

`--action apply --apply` affects shared target settings across hosted domains and may reload Postfix. All known target conflicts
are checked before writes. API requests are journaled and uncertain results require readback; missing results are not resent.
Changed files have private backups and effective/map checks. Failed configuration writes attempt rollback only while current
values still match the operation; failed rollback retains a pending journal for reconciliation. Applied choices remain pinned.

`--action check` verifies the selected target settings and approved administrator identities. Domain staging, freezing, transfers,
activation and webmail import require this matching source/target receipt and fresh target readback. Plesk mail-domain migration
does not import panel-wide policy through this PostfixAdmin preparation path.

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
records `target_verified: false` and `selection_recorded: false`: apply and verify the matching shared settings through `server-settings`.
Its separate receipt records selections, exclusions, relay and administrator decisions. `transfer-plan.json` records the volume, vmail identity,
capability checks and mailbox preview with `transfer_ready: false`. It does not verify account/transfer journals, stopped writers,
activation state or available disk capacity. Paths and append compatibility must be rechecked under isolation before copying.
Mail execution, webmail import and handoff are separate guarded commands, also coordinated by `migrate`.
Before-phase assessment and the first activation attempt compare fresh source account/filter evidence with the original plan.
PostfixAdmin comparisons ignore SQL row order and calculated target quotas; changed sections stop continuation with private evidence.
Plesk checks its complete account/filter snapshot. These comparisons complement the stopped-writer requirement. Plans record a digest for
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

Planning and account staging check that Dovecot advertises every password scheme needed by the batch. PostfixAdmin accepts
complete MD5-CRYPT hashes; Plesk retains MD5-CRYPT, SHA256-CRYPT, SHA512-CRYPT and BLF-CRYPT, with available plaintext converted
to salted SSHA256. Unsupported or malformed hashes and password payloads requesting a reset stop continuation.
Applied staging compares all stored mailbox hashes with the plan in one native database readback before recording completion.
Activation repeats this check under isolation. A mismatch stops the operation without rewriting passwords.

Recovery email addresses retain their source spelling and are read back through the API. Invalid or self-referencing addresses,
phone/SMS recovery and contacts without a target mailbox remain blockers. The saved password-expiration decision must match
the planning assessment: only the reviewed disabled policy can continue. Enabled recurring/datetime expiration and unresolved
review remain blocked; a forced password change is not used as a substitute.

New planning manifests fingerprint the normalized saved policy. Account application also pins its fingerprint in the journal
before writes and checks it on retries and during activation. Existing batches without a planning-policy fingerprint must still
match their saved expiration assessment; their first applied continuation pins the policy. Keep the original policy with the
batch. Changing the configuration's policy file does not replace the batch's saved decisions.

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
aliases, alias domains, BCC and recovery emails with the saved plan and recorded activation phase, including interrupted activation.
It does not test password login, messages, filters, sender permissions or delivery. Message copying and guarded activation are
available separately, with source isolation and resumable guided migration. Only this application's planning and
account-journal formats are accepted. Keep other operation journals with the tool that created them.

To explicitly compare stored mailbox hashes with the original plan, add `--passwords` to `verify`. It also checks required
Dovecot schemes and performs no password writes. Subsequent user password changes intentionally report a mismatch with this
option; ordinary verification and an already completed activation do not enforce the original hashes. Hash comparison is not
an IMAP/SMTP login check, so controlled client authentication and delivery checks remain part of handoff.

## Account activation

After completing staging, explicit sender grants, prefilter staging, the final mail pass and Sieve installation, preview activation:

```bash
mailcow-migrate activate --config migration.json --plan /root/mailcow-in-progress/plans/example.org/first/plan.json \
  --export /root/mailcow-in-progress/exports/example.org/EXPORT_ID --ready --writers-stopped
```

Both confirmations are required for preview and apply. `--ready` confirms review of the batch and pre-activation cutover checks;
`--writers-stopped` confirms batch writes, submission and account administration are stopped on both servers. The command runs
as root on the Linux Mailcow host and supports both sources. It rechecks account/recovery inventories, sender grants,
final mail/Sieve receipts, source login/delivery/session isolation, target access/delivery holds and the saved export/webmail proposal.
Unsupported mappings, changed target IDs, unresolved writes or lost isolation block further edits.

Add `--apply` to temporarily enable the domain, activate the reviewed forwarding/vacation prefilters, restore each mailbox and
alias/alias-domain's planned activity, enable planned BCC records, then restore the domain's planned activity. Source-inactive
accounts remain inactive. Native SOGo handoffs retain their recorded prefilter activity instead of enabling duplicate rules.

Activation pins the exact ordered requests and target IDs in `state.json`. Each edit records intent first and is read back.
Rerunning with the same plan resumes from receipts; an uncertain write is accepted only when its requested target value can be
verified. Missing or mismatched outcomes remain pending. Preview and `verify` may update receipts for confirmed earlier requests
without sending account edits. Keep all journals together; staging and transfers cannot continue once activation starts.

The completion marker is saved only after account, sender, filter and isolation checks succeed. Target protocol access, SOGo
maintenance controls, sync-job pauses and delivery holds remain in place; source controls remain in place too. `verify` and
`target-isolation --action check` can inspect this activated-but-isolated batch. Recalculate quota, then complete webmail import
and its verification before target release. Continue with certificate/signing setup, source forwarding and handoff verification afterward.

## Quota recalculation

After account activation and before webmail import, preview quota recalculation while both servers remain isolated:

```bash
mailcow-migrate quota --config migration.json --plan /root/mailcow-in-progress/plans/example.org/first/plan.json --writers-stopped
```

Add `--apply` to run `doveadm quota recalc -u USER` inside `dovecot-mailcow` for each planned active mailbox. This rebuilds usage
counters after copying mail. Disabled users are skipped and need controlled recalculation before any future activation; a
disabled primary domain blocks the command. The PostfixAdmin activation/isolation path is required.

The command checks final mail/Sieve receipts, activated account inventories, live source/target holds and the Dovecot mail volume
against the root saved during transfer. Native output, warnings and errors remain visible; recalculation has no imposed timeout.
Preview runs no recalculation commands and writes no quota receipt.

Each applied attempt clears prior quota completion before live checks, then records the current user before its native command.
The private `quota.json` binds progress to the plan, source/target, completed transfer fingerprint and mail volume. Completion
requires successful command exits for every selected mailbox and renewed readiness checks; it is not an independent comparison
of reported usage values. Failures or interruption retain incomplete/pending state. Rerunning `--apply` repeats all selected
recalculations under fresh checks; it does not assume the interrupted command succeeded.

Webmail import and target release require a completed matching quota receipt and the same live mail volume. Quota work retains
isolation. Import or verify domain administrators, then complete webmail import before explicitly releasing target access and delivery.

## Domain administrators

Preview the administrator mappings and target memberships from the saved account proposal:

```bash
mailcow-migrate admins --config migration.json --plan /root/mailcow-in-progress/plans/example.org/first/plan.json
```

The command reuses the reviewed `adminNames`, `allDomainAdmins` and `mergeAdmins` policy. Email-shaped source logins map to
lowercase dotted names unless explicitly renamed. Invalid/reserved names, collisions, unresolved ALL-only grants and alias-domain-only
memberships block import. An approved `batch-only` ALL mapping grants only this primary domain. Source global administrators
are excluded and reported for separate shared-server review; Plesk panel administrators are outside this mail-domain step.

An existing renamed login requires explicit ownership approval in `mergeAdmins`, unless this journal proves the account was
created by the migration. Adding membership to any existing administrator also requires `mergeAdmins`. Existing activity must
match the proposal. Merges retain all current domains and preserve `force_tfa` and `force_pw_update`; they keep the existing password.
Keep target account administration frozen while applying these reviewed changes.

Add `--apply` after account activation. The entire selection is inspected before writes. New users receive strong random
passwords saved privately in `admin-credentials.json` before creation; credentials are reused on retry and never printed.
Retain that file with `admins.json` and deliver credentials separately after the migration controls permit access. Preview
generates no passwords and sends no API writes. It may update local receipts when an earlier uncertain request is confirmed.

The journal pins mappings, merge decisions and preserved memberships. Pending requests are compared with the exact saved
payload; uncertain creations also require native password-hash verification before being accepted. Missing or mismatched
results stay pending. Applied changes verify exact memberships/activity and the security values carried by each merge.
Disappeared administrators or lost completed grants stop continuation rather than triggering replacement or password resets.

`--check` is read-only and verifies completed grants and preserved memberships. Later password/MFA changes are not frozen by
the completion receipt; this check does not exercise interactive administrator login. Webmail import requires completed,
matching administrator receipts whenever the batch has mapped domain administrators.

## Sender permissions

After account staging, preview the explicit `senderPermissions` mappings from the saved policy:

```bash
mailcow-migrate senders --config migration.json --plan /root/mailcow-in-progress/plans/example.org/first/plan.json
```

The policy must still match the saved account proposal. Each mapping names a real batch mailbox and explicit batch mailbox
or alias addresses; new wildcard/domain-wide grants are not accepted or inferred from source aliases. Existing target ACLs
are retained, including any broader grants already present.

Coverage recognizes the mailbox's own address, existing exact/global/domain ACL entries, and target aliases whose
`sender_allowed` flag and destinations permit that mailbox. Already covered requests need no mailbox edit.

Add `--apply` to merge missing explicit addresses. All selected mailboxes must remain staged inactive, with completed
account receipts and no activation or uncertain account write. The command checks the entire selection first, saves original
grants in a private `senders.json` journal, and records API intent before editing. Edits carry forward `force_tfa` and
`force_pw_update`; unrecognized security values or changes after inspection stop the edit.

Readback verifies requested coverage, retained grants and security values for each edit. An uncertain API outcome is reconciled
against the exact saved request before any further write; preview can update those local receipts without changing mailboxes.
Missing preserved grants or drift after a completed permission edit block continuation rather than reapplying it.

`--check` is read-only and requires completed sender staging when mappings are configured. It can run after account activation
and rechecks requested coverage and preserved grants. It does not freeze later password/MFA choices. This verifies API permission
data; SMTP authentication/enforcement and client delivery tests remain separate. Sender staging neither activates mailboxes
nor changes Postfix service restrictions.

## Forwarding/vacation prefilter staging

After account staging, preview the inactive Mailcow prefilters from the saved webmail plan:

```bash
mailcow-migrate prefilters --config migration.json --plan /root/mailcow-in-progress/plans/example.org/first/plan.json --export /root/mailcow-in-progress/exports/example.org/EXPORT_ID
```

The command reloads the planning export and saved policy, then reproduces the webmail proposal using its original assessment
time. An elapsed vacation window does not silently change a previously reviewed staging plan. Changed proposals, unsupported
data and unresolved forwarding/vacation decisions block staging. Completed account receipts and matching inactive target
accounts are required; activation or an uncertain account write stops the operation.

Add `--apply` to create missing prefilters with `active: 0`. A unique, exactly matching inactive filter is retained without
another write. Different script contents, duplicate descriptions or any active prefilter for a selected mailbox block the
operation. Unrelated inactive filters are retained. All selected filters are checked before the first API mutation.

The private `prefilters.json` journal binds the plan, source/target, policy choices, frozen planning time, forwarding mappings
and Sieve evidence fingerprint. Each API request is recorded before sending it. A later run reads Mailcow to reconcile an
uncertain request; it does not blindly repeat it. Missing, ambiguous or mismatched outcomes remain pending. Preview performs
no filter API writes, but it can update local receipts for an earlier request confirmed on the target.

Completion requires exact script/metadata readback, inactive status and distinct target IDs for every selected prefilter.
The source-script fingerprint uses the same calculation as Sieve installation so later activation can compare their evidence.
This step currently supports PostfixAdmin and does not enable filters, modify existing filter contents, import native SOGo
settings or authorize account activation. Prefilter staging is available before source/target isolation because it only adds
disabled entries; mail/Sieve transfers still require their stopped-writer confirmation.

### Prefilter activation backend

The internal activation backend requires completed staging, final mail and Sieve receipts, and matching staged/copied Sieve
fingerprints. It checks each saved filter ID, owner, type, script and activity, rejecting duplicate matches or other active
prefilters for that mailbox. Forwarding uses its planned activity; staged vacation-only filters are enabled with their date guards.

Activation requests share the private `prefilters.json` journal and record only an `active` edit before sending it. Each applied
edit is read back. An uncertain request is reconciled only when the live filter still matches its saved identity and requested
activity; otherwise it remains pending. Preview may record a confirmed earlier request, but performs no API writes. A separate
read-only mode checks recorded activity without reconciling pending writes.

Verified native SOGo settings receipts take precedence: retained forwarding prefilters keep their imported activity, while
prefilters replaced by native settings stay disabled. An uncertain native settings write blocks activation. Native settings
import also accepts valid earlier prefilter activation receipts, preserving the order of the eventual account/webmail workflow.

The `activate` command calls this backend under the workspace lock, checking account phase, stopped writers and source/target
isolation. There is no standalone filter-activation command. Full webmail completion and release remain separate.

## Mail copying

After staging accounts, pausing target access/delivery, freezing source logins and pausing source delivery with RoundCube sessions
revoked, preview a PostfixAdmin batch:

```bash
mailcow-migrate mail --config migration.json --plan /root/mailcow-in-progress/plans/example.org/first/plan.json --writers-stopped
```

`--writers-stopped` is required for preview and apply. Supply it only after stopping batch writes, submission and account
administration on both servers. The command rechecks the existing login, delivery, queue, session and detected-writer controls;
custom writers hidden behind scripts still require operator action. PostfixAdmin SMTP authentication and session draining are also checked.
Resolve saved blocking
findings first. Plesk copying remains unavailable until its isolation adapter is implemented.

The command requires matching completed account receipts, inactive target accounts and a completed target pause, with no
uncertain account request or activation started. It verifies the account inventory and source domain/mailbox inventory,
resolves the current mail volume and vmail ownership, and probes sender/receiver rsync capabilities. Source host/root must
match quota measurement and any previous transfer. All source Maildirs are checked before the first copy.

Add `--apply` to copy missing files. Mailboxes with existing target data first pass a checksum dry run; content/type conflicts,
target symlinks and message IDs found under another target path stop continuation. Copying uses
`--ignore-existing`, preserves hard links and safe-link handling, and excludes Dovecot indexes, `maildirsize` and temporary
message files. Required target directories are created with vmail ownership. A read-only count precedes each rsync pass;
entry names, metadata, totals and transfer progress are displayed, with command errors stopping the operation.

Every applied attempt records `mailPending: true` and clears `finalMail` before live preflight. The private `transfers.json`
pins the plan, configured source/target, mail volume and ownership. An interrupted pass keeps its journal and already copied
mail; rerun the same command to repeat live checks and append only missing files. Preview writes private diagnostics only,
leaving target mail and transfer receipts unchanged. If the mailbox parent is absent, preview reports the required directory
creation without attempting a copy into a nonexistent parent.

Use `--apply --final` for the final mail pass. Only successful copying followed by live isolation, source inventory and target
storage checks records `mailPending: false` and `finalMail: true`. An ordinary applied pass records `finalMail: false`.
No Sieve completion is inferred from mail copying. Disk capacity, fixed-quota suitability, complete account-setting drift
verification, the separate Sieve step, quota recalculation, activation and cutover remain separate requirements. Keep both servers
isolated after copying; a final mail receipt does not authorize access restoration or mark the migration complete.

## Sieve installation

Use the completed source export selected during planning and the same stopped-writer/isolation prerequisites as mail copying:

```bash
mailcow-migrate sieve --config migration.json --plan /root/mailcow-in-progress/plans/example.org/first/plan.json \
  --export /root/mailcow-in-progress/exports/example.org/EXPORT_ID --writers-stopped
```

The default action previews the entire batch without writing, compiling or removing target scripts. It validates the export's
completion manifest, file hashes, domain/source identity and saved export ID. Missing inactive-script inventory, additional
inactive scripts and unsupported PostfixAdmin filter syntax block installation before target writes.

Personal scripts are selected when present, including empty scripts. Add `--use-default-sieve` to select the exported default
for mailboxes without a personal script. Without that flag, those mailboxes use Mailcow's default filtering and their personal
script/binary must be absent. The selection does not change Mailcow's shared default script.

Conflicting target scripts, or stale compiled files when no source script is selected, block the step unless
`--replace-sieve` is supplied. Preview the intended selection/replacement, then add `--apply` to install it. Conflicting script
and binary bytes are backed up under a private `sieve-backup-*` directory before replacement or removal. Backups are checked
against the inspected bytes and recorded in the transfer journal; reruns retain earlier backups.

The whole batch is inspected first. Paths must stay within the export or existing target Sieve directory and use regular files
and directories, including ancestors of missing files. Symlinks and changed files block continuation. Installed script bytes
are preserved, owned by vmail with mode 600, and compiled using `sievec` as vmail inside `dovecot-mailcow`. Installed bytes and
the compiled file are checked afterward. Selecting target default filtering removes only the inspected personal script and
its compiled file, with backup/replacement permission when they conflict.

Each applied attempt clears `sieve` and sets `sievePending` before export, container or filesystem checks. Success records the
exported script fingerprint and default-selection choice, then sets `sieve: true` and `sievePending: false` after renewed
isolation/storage checks. Mail completion fields are retained separately. Failures leave the pending state and any backups;
reruns inspect the actual files again, and any new conflict still requires explicit replacement permission.

This step supports both sources. It installs exported filters but does not import editable SOGo filters, apply
vacation/forwarding settings, recalculate quota, activate accounts or release isolation. Those steps and full source-drift
verification remain separate; keep source administration and user edits frozen while using the saved export.

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

The PostfixAdmin sequence is RoundCube proposal review/deferral and draft confirmation, SOGo target preparation,
source login freeze, then source delivery pause.
`source-delivery --action pause --apply` installs and verifies delivery holds before revoking RoundCube sessions. It includes
all profiles belonging to batch mailboxes, including duplicate and alias-domain logins, while retaining unrelated sessions.
The helper matches decoded ownership and deletes each session only if both its ID and stored serialized value still match;
changed sessions stop the operation. It then checks for zero remaining batch sessions and rereads the persistent snapshot.

`roundcube-sessions --action revoke --apply` can resume this session step separately after the same live login and delivery
checks. `--action check` verifies completed revocation without changing sessions. Plesk revocation uses its native source-isolation checks.
No operation changes contacts, identities or preferences on the source,
imports data into SOGo, copies mail or activates accounts.

Private evidence lives under `roundcube-sessions` in the batch directory. Its journal records the source, target, plan,
installation, baseline snapshot, proposal review/deferrals, draft confirmation and each attempt's before/removed/after counts. Snapshots are retained
by fingerprint, including changed snapshots found during verification. An interrupted revocation may already have committed;
resume against the same baseline after restoring the required isolation. Preview cannot replace an unfinished operation's
baseline, and persistent-data drift requires reconciliation. Existing journals without proposals can add the new review metadata
through preview while retaining their session receipts and unchanged baseline. Invalid exporter output is retained privately for diagnosis.

Session completion is distinct from webmail-import completion: this journal retains `import_complete: false` and
`source_isolated: false`, and cannot satisfy Mailcow's webmail-import release guard. Proposals retain `target_verified: false` and
`import_complete: false`. SOGo preparation, guarded import/readback and combined source checks are separate operations;
session revocation alone does not establish migration readiness.

## SOGo target preparation

Use the reviewed, unchanged RoundCube proposal to preview target requirements:

```bash
mailcow-migrate sogo-target --config migration.json --plan /root/mailcow-in-progress/plans/example.org/first/plan.json
```

The default `preview` action checks `sogo-tool`, GNUstep property-list conversion and effective settings from
`sogo-tool dump-defaults all`, including domain overrides. It saves a private assessment and any proposed configuration change.
Missing SOGo user sources are reported separately: configuration readiness does not mean that inactive staged users are ready
for native backups or import. An empty selected-profile inventory needs no SOGo changes.

If custom identities are disabled, review the preview before running `--action configure --apply`. This sets
`SOGoMailCustomFromEnabled = YES` in the persistent configuration and in affected existing overrides for this batch, preserving
other parsed settings and unrelated domain overrides. **It changes the default for all SOGo users and restarts sogo-mailcow,
briefly interrupting webmail/DAV for all domains.** Apply only when that interruption is acceptable. If the required setting is
already enabled and no configuration operation is pending, the command does not write or restart anything.

The saved preview must still match the proposal and live configuration. A private backup and pending operation record are
written before atomic replacement, retaining the configuration file's owner and permissions. The command waits for SOGo's
listener after restart and reads back the effective identity setting. A failure retains the pending record; rerun the same
configure command to resume only when the live file matches its saved before/after values. External drift stops continuation.
Backups, previews and diagnostics are private under `sogo-target` in the batch directory and can contain configuration secrets.

`--action check` rechecks the required effective configuration. Source login freeze and source delivery pause also require these
checks, and refuse an unfinished SOGo configuration/restart. Source login release remains available if target preparation fails.
This configuration step does not import identities, preferences or contacts.

When domain user sources need loading or SOGo has cached an unknown user, run:

```bash
mailcow-migrate sogo-target --config migration.json --plan /root/mailcow-in-progress/plans/example.org/first/plan.json --action prepare-users --apply
```

This action requires the reviewed source snapshot/folder trees to remain unchanged, no uncertain account request, and completed
SOGo configuration preparation. If selected domains lack effective user sources, it restarts `sogo-mailcow`, waits for its
listener and rechecks the sources and required settings. **The restart briefly interrupts webmail/DAV for all domains.**
Already loaded user sources need no restart. This does not activate staged accounts; if sources remain missing, inspect the
startup logs, domain activation and domain overrides before retrying.

Before each native backup, it checks the selected mailbox's `username+attributes` Memcached entry through `php-fpm-mailcow`.
Only a string whose trimmed value is exactly `null` is deleted. Other values and other cache keys are retained. Cache errors
stop that user's preparation. A fresh backup must then pass the same preference, address-book and folder-state checks as
`inspect-users`; clearing a cached lookup alone does not establish readiness.

Each run retains a private attempt record before restarting or checking cache entries, with per-user outcomes and any unfinished
step. A retry checks live sources and cache values again; it does not rely on an earlier success or overwrite earlier attempt
records. The first native backups and diagnostics remain available. Preparation does not import data or authorize target release.

`--action inspect-users` rechecks the approved source snapshot and source folder trees, then reads native SOGo backups as the
`sogo` user once the domain's user sources are available. The first
private backup for each user is retained. Failed native operations retain their private temporary directory inside the container
for diagnosis. Existing preference differences are saved in private conflict reports; comparison normalizes only the supported
boolean flags, preserving names, signatures, missing fields and identity/filter ordering.

For profiles with saved folder display state, inspection also reads the target Dovecot folder tree. Every source folder must be
present on Mailcow, including parent folders. The proposal maps collapsed folders to SOGo's primary-account identifiers and
expands the remaining target folders, including any additional target folders. It preserves modified UTF-7 names and the
supported namespace/separator rules. Missing folders block the assessment until mail has been copied.

Existing `Mail.ExpandedFolders` values are accepted as arrays or JSON-encoded arrays and compared as sets for the primary
account. A different primary-account selection or malformed settings block the assessment; unrelated Mail settings and
other accounts' folder identifiers are retained in the proposed value. Equivalent existing selections need no write.
The private user assessment records the target tree, proposed Mail settings and whether a later write would be required.
It does not create folders or establish that messages have been transferred. Recheck source/target trees and native settings
under isolation before a future import; this assessment is not an import receipt.

An existing destination address book without this application's import journal is a conflict, even if its contents appear to
match. Native inspection never adopts that book or overwrites conflicting data. Read-only inspection reports missing user
sources or failed lookups; the separate `prepare-users --apply` action can refresh sources and reconcile unknown-user caches.
Configuration/user assessments retain `import_ready: false`; account activation and the guarded webmail command establish the later prerequisites.

## Webmail import

After account activation and quota recalculation, while source and target remain isolated, preview the approved webmail import:

```bash
mailcow-migrate webmail --config migration.json --plan /root/mailcow-in-progress/plans/example.org/first/plan.json \
  --export /root/mailcow-in-progress/exports/example.org/EXPORT_ID --writers-stopped
```

The command requires final mail/Sieve and quota receipts, completed account activation, matching recovery/sender/administrator inventories, paused
source logins/delivery/sessions and target access/delivery controls. It reloads the approved RoundCube snapshot/proposal and
saved webmail plan, including accepted preference deferrals. Changed inputs or unsupported mappings stop the operation.
Prepare required SOGo configuration and user sources through `sogo-target` first; the import does not restart shared services.

Preview checks existing contacts/preferences/folder state and editable filters, retaining private backups without importing
user data or creating a completion receipt. Vacation/forwarding target checks run when editable-filter import is already complete;
otherwise preview explicitly defers those checks until that prerequisite has been applied.

Add `--apply` to import RoundCube preferences, contacts and folder state. PostfixAdmin then imports editable SOGo filters and native
vacation/forwarding settings, in that order. Plesk keeps its copied personal Sieve and runs only the RoundCube stage.
Every backend keeps its own conflict checks, pending receipts and native readback. Already completed
stages retain subsequent user edits. Interrupted RoundCube/filter writes are inspected before resuming; uncertain native settings
writes require reconciliation before another attempt. Source data remains unchanged, and approved deferrals remain visible.

The private `roundcube/state.json` binds the batch, source/target, export, approved source/proposal fingerprints and the source-specific
backend receipts. Completion is recorded only after every required stage completes and source, account, sender, isolation and prefilter
handoff checks pass. A completed rerun verifies saved receipt integrity and leaves live user data untouched.

Webmail completion leaves isolation paused. After reviewing the results, use `target-isolation --action release --apply` with
the same config and plan to restore saved target access, delivery and sync jobs. Release requires this full completion receipt
and unchanged, matching backend and quota receipts; a missing, partial or foreign record cannot authorize it. Source isolation
stays in place. DNS/source routing, operator client/delivery checks and full migration completion remain separate.

### Native import backend

The SOGo data backend supports journaled preference, contact and folder-setting writes with native readback. The `webmail`
command supplies the shared lock, completed transfer/activation checks, live source/target isolation checks and source
snapshot/folder verification. Preparation commands remain separate from importing user data.

The backend checks every selected user's existing data before importing any preferences or contacts. It retains the first
private native backup and records each preference write before execution. Contact imports use stable record IDs, chunks of
50 and additive `sogo-tool restore -f`; each chunk is read back before proceeding. A matching import journal is required to
recognize an existing destination book. Conflicting data, changed metadata/sharing and unknown records block continuation.

Folder-setting receipts preserve unrelated Mail settings and other accounts' identifiers. Source/target folder drift,
changes during a partial import and disappearance of a completed setting block continuation. Interrupted writes are
reconciled against fresh native backups; a completed native import retains subsequent target user edits.

Native operations share private temporary-file handling, retaining files on failure and removing only the expected file
and empty directory after success. The separate `sogo-target/native-import-state.json` records `native_complete` for this
backend and retains `import_complete: false`. This receipt alone cannot satisfy the full webmail-import or target-release guards.

### Editable filter import backend

The internal editable-filter importer supports the reviewed PostfixAdmin Sieve subset. It loads the saved plan's export,
rejects unsupported or inactive source scripts, and checks every mailbox before changing any filter. The active target script
must match the exported personal/default script; an existing stored `sogo` script must match too. Comparisons normalize CRLF
and trailing whitespace. Existing editable rules must be absent, empty or equivalent to the converted rules. Enabled native
Vacation, Forward or Notification settings block replacement until reconciled.

The importer retains private native backups, stores missing `sogo` scripts through `doveadm sieve put`, and restores only the
preferences record through XML `sogo-tool restore -p`. It changes `SOGoSieveFilters` while preserving all other defaults and
the complete settings dictionary, then activates the stored script and verifies preferences, stored contents and active contents.
XML encoding preserves boolean/numeric types and carriage returns; unsupported control characters fail during preflight.

The private `sogo-filters/state.json` pins the plan, target, export and converted-source fingerprint. Each user has a pending
receipt with original preferences before any write. Interrupted operations inspect live values before continuing; conflicting
scripts or unrelated preference drift block continuation. `filters_complete` is recorded only after all users pass readback.
Once complete, subsequent target user edits are retained. This receipt keeps `import_complete: false` and does not authorize release.

The `webmail` command supplies the workspace lock, final transfer/activation checks, live isolation and unchanged-source checks,
with explicit stopped-writer confirmation. Native vacation/forwarding settings use the next backend below. Plesk webmail import
retains its separate personal-script copy/compile path and does not run this editable-filter stage.

### Native vacation and forwarding backend

The internal settings importer uses the saved webmail proposal and policy, reproduced from the original export at its planning
time. It requires completed editable-filter import and validates any existing prefilter staging journal against the same plan,
target and evidence. It checks all remaining users before changing preferences, scripts or prefilters.

Existing Vacation/Forward settings must be empty or equal to the proposal. Enabled settings absent from the proposal and
enabled notifications block regeneration. The editable rules and active/stored Sieve must still match the export. Both the
SOGo and Dovecot containers must render each vacation boundary in the policy's `vacationTimeZone`, including inactive vacations.
Supported date windows, reply intervals and enabled states come from the reviewed proposal; reply history is not transferred.

For each user, the backend saves a private snapshot and pending receipt, updates only a matching staged prefilter, and restores
the proposed preferences while preserving other defaults/settings. Forwarding remains in the Mailcow prefilter when personal
rules must control the local copy; otherwise the staged forwarding/vacation prefilter is disabled as native settings take over.
Active editable rules or native Vacation/Forward settings trigger SOGo script generation through its installed Sieve credentials.
Readback checks the complete preferences, active stored script, enabled vacation date guards and any changed prefilter.

The private `sogo-settings/state.json` records each verified user's settings/script and sets `settings_complete` only at the end.
Any pending user blocks another attempt until the uncertain operation and backups are reconciled; writes are never blindly
repeated. Completed imports retain subsequent user edits. The receipt keeps `import_complete: false` and does not authorize release.

The `webmail` command calls this backend last, under the shared lock and the final transfer/activation, isolation and source checks.
Its completed receipt contributes to full webmail completion; target access release remains a separate explicit operation.

## PostfixAdmin source logins

Preview the login-control step using the saved plan and configuration:

```bash
mailcow-migrate source-logins --config migration.json --plan /root/mailcow-in-progress/plans/example.org/first/plan.json
```

The default `preview` action checks the configured source, required commands, effective Dovecot passdb and supported SQL query.
It identifies whether the exact batch exclusion already exists and rejects exclusions that partially overlap these users.
Only a single SQL passdb using `/etc/dovecot/dovecot-mysql.conf` is supported. The base password query must match the supported
form; existing migration exclusions for other batches are retained. Wildcard mailbox names are rejected before remote commands.
The plan's source connection must match its planning manifest and current configuration. Plesk uses `source-isolation`;
an alias-only batch with no mailbox logins needs no login operation.

After users have saved drafts and closed clients, `--action freeze --apply` excludes exactly the planned mailbox usernames,
reloads Dovecot, clears their authentication caches and disconnects their sessions. Before changing logins, it checks the
delivery prerequisites described below and the saved RoundCube snapshot/session preview with draft confirmation.
Required SOGo target settings must also pass their live preflight, with no configuration restart pending.
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
Source delivery pause also verifies the supported SMTP authentication path; a successful login freeze alone does not establish
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

There is no general delivery rollback. Holds remain until guarded source forwarding is applied; releasing
source logins alone does not remove them. The delivery journal explicitly records `source_isolated: false` and
`webmail_sessions_verified: false`; session verification is recorded separately in the RoundCube session journal.
Combined source readiness also requires the separate submission and RoundCube receipts. Delivery receipts alone do not authorize
copying, activation or cutover; the mail command additionally checks target isolation and requires stopped-writer confirmation.
Plesk uses the separate native `source-isolation` adapter.

## Target isolation

For Plesk, prepare/review the RoundCube snapshot and SOGo requirements, then use the separate source isolation command:

```bash
mailcow-migrate source-isolation --config migration.json --plan /root/mailcow-in-progress/plans/example.org/first/plan.json
```

`--action freeze --apply --writers-stopped` requires saved drafts and reviewed proposals. It preserves source settings, installs
the Dovecot denial and Postfix submission/recipient/transport controls, drains old workers, disconnects sessions and revokes
RoundCube sessions. Source account/filter snapshots must remain unchanged. `--action check` verifies isolation and sessions.
`--action release --apply` restores source access/delivery only before activation and with no uncertain account request;
it retains target staging/isolation. Keep the local `source-isolation.json` and remote recovery state together.

The supported Plesk path requires Dovecot 2.4, full-address logins, Postfix 3 or newer with regexp/retry support, and no unsupported
username rewrites or service transport overrides. Shared settings and other batches remain preserved. Interrupted changes retain
their pending operation and backups. After source isolation and inactive account staging, pause the target and use the same
`mail`, `sieve`, `activate` and `quota` commands. Plesk Sieve is read from its verified JSON export and compiled natively; forwarding
with an existing personal script remains blocked to preserve ordering. Plesk webmail import follows activation and quota verification.

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

`--action release --apply` restores saved access, delivery and sync-job settings only after matching receipts confirm completed
activation, quota recalculation and the full webmail import. Initial isolation refuses activation in progress, a released batch
or an interrupted release. Isolation never copies mail or activates accounts; source controls and handoff use their separate guarded commands.

### Target isolation for unfinished webmail import

For an activated batch with a missing or incomplete webmail import, preview a separate isolation phase:

```bash
mailcow-migrate target-isolation --config migration.json --plan /root/mailcow-in-progress/plans/example.org/first/plan.json --webmail-repair
```

This requires completed target restoration recorded in the original journal for the same plan and target, with no pending
operation. Completed webmail imports cannot be restarted. The private `webmail-target-isolation.json` journal saves current
protocol controls, sync-job activity and configuration independently, retaining the original restoration record. Existing guard
files can be reused only when their content matches the intended guard or the original journal's recorded final content.

Add `--action pause --apply` to establish isolation; retain `--webmail-repair` when resuming that pause. The same delivery,
webmail, session and writer checks apply. Once the repair journal exists, `--action check`, `--action release --apply` and
import readiness checks select it automatically. An incomplete pause, interrupted release or released repair journal cannot
fall back to the original journal to authorize further import work.

With source isolation and all import prerequisites still verified, resume the `webmail` command. Completed native
stages retain subsequent user edits. After full webmail and quota completion, release restores the settings saved before this
repair phase. Source controls remain separate for each adapter.

If source RoundCube sessions need revocation after activation, use `roundcube-sessions --action revoke --apply --webmail-repair`
with the original config/plan and fresh `--drafts-saved --writers-stopped` confirmations. Account activation and target isolation
must verify, the import must be unfinished, and the approved source snapshot must remain unchanged. This step changes sessions
only; it does not recopy mail or restart completed imports.

Source recovery is limited to the pre-activation boundary. Activation intent, uncertain writes, missing account evidence after
target isolation, or a forwarding/completion record block release. PostfixAdmin login release retains separate delivery holds;
Plesk release restores its saved access/delivery controls. Neither action is a general rollback or copies target mail back.

## Forwarding and final handoff

After webmail completion and target access restoration, use the original configuration and plan:

```bash
migration_plan=/root/mailcow-in-progress/plans/example.org/first/plan.json
mailcow-migrate certificate --config migration.json --plan "$migration_plan"
mailcow-migrate certificate --config migration.json --plan "$migration_plan" --action apply --apply
mailcow-migrate signing --config migration.json --plan "$migration_plan" --signer mailcow --apply
mailcow-migrate source-forward --config migration.json --plan "$migration_plan" --target-ip 192.0.2.10
mailcow-migrate source-forward --config migration.json --plan "$migration_plan" --target-ip 192.0.2.10 --apply
mailcow-migrate handoff --config migration.json --plan "$migration_plan" --action check
```

Use the real target address reachable from the source on TCP 25, including a routed private address when appropriate.
Keep the configured source SSH hostname stable through DNS changes. Forwarding rejects source/loopback destinations and
verifies accepted, rejected and catch-all recipients without sending a message during its SMTP probes. Applying starts real
queued delivery, retains source login/submission blocks and pins the destination. Source maps, queued-recipient history and
other domains are preserved; ambiguous expansion/history or shared-map drift blocks continuation.

Certificate setup defaults to `mail.DOMAIN`; `--hostname` selects the intended client name. Its A/AAAA records must identify
Mailcow, not the source. Preview explains shared nginx/ACME effects. Application preserves other configured names, backs up
the configuration and records each stage before changes. Separate certificates retain their HTTPS virtual-host selection;
custom issuers require their own certificate manager. Interrupted requests retain all backups. `--action check` waits without
restarting services; use `--action retry --apply` only after correcting the cause. All five endpoints must pass twice at least
10 seconds apart. Configuration preview and TLS status do not claim that issuance has succeeded.

For provider signing, choose `--signer relay --dkim-name selector._domainkey.example.org --relay-id ID` and provide `--cname`
or `--public-key` with its `--key-type`. Active provider CNAMEs are discovered when available and retained as delegations so
provider keys can rotate. Mailcow signing creates only a missing 2048-bit key after `--apply`; an existing or changed key is
never reset. The selected domain relay and mailbox inheritance must verify. Publish the displayed DNS records yourself.

Handoff retains first DNS observations and TTLs, verifies authoritative MX/A/AAAA agreement for all batch domains, explicit
Mailcow IP coverage in one SPF policy, and the selected DKIM record. SPF includes/a/mx mechanisms are retained but not expanded.
It also checks every mailbox's readable folders/message counters and both queues. Source batch queues and deferred/held target
delivery block completion. These checks do not prove every message's content or actual received-message authentication results.

After controlled inbound/outbound, alias/catch-all, client, attachment/webmail, sender-permission/BCC and received SPF/DKIM/DMARC
checks have succeeded, record the operator confirmations:

```bash
mailcow-migrate handoff --config migration.json --plan "$migration_plan" --action complete --apply \
  --delivery-confirmed --clients-confirmed --message-auth-confirmed --permissions-confirmed
```

Completion reruns live checks and requires completed imports, activation, quota, source forwarding and restored target access.
The first completion and its immutable evidence snapshot are retained; later checks write a separate current report. Keep source
forwarding and all recovery evidence until cached DNS routes expire. No command edits DNS or offers a general post-cutover rollback.

## Package layout

| Module | Responsibility |
| --- | --- |
| `cli`, `wizard`, `configuration` | Entry point, interactive setup, immutable settings, and shared validation. |
| `preparation`, `assessment`, `workflow`, `source_drift` | Guided preparation/migration, phase reports, pinned decisions and fresh source comparisons. |
| `inspection`, `core.dns`, `sources.inventory` | Read-only source inventories, authoritative DNS agreement, and inspection reports. |
| `exporting`, `sources.export` | Private source exports, inventory rechecks, completion manifests and file hashes. |
| `planning`, `plans`, `sources.sql`, `sources.usage` | Account mapping, policy decisions, export validation and measured quotas. |
| `accounts`, `plans.saved`, `targets.account_inventory`, `targets.reconciliation` | Saved-plan validation, inactive account staging, receipts and readback. |
| `isolation`, `targets.isolation`, `targets.writers` | Target access/delivery isolation, writer checks, recovery journals and guarded restoration. |
| `source_logins`, `sources.logins` | PostfixAdmin login-query exclusions, source configuration backups, session checks and guarded release. |
| `source_delivery`, `sources.delivery`, `sources.php` | PostfixAdmin delivery holds, recipient identity routing, BCC filtering and queue safeguards. |
| `roundcube_sessions`, `sources.roundcube` | Source webmail snapshots, session ownership, draft confirmation and revocation receipts. |
| `sogo_target`, `targets.sogo`, `targets.sogo_preparation`, `targets.sogo_inventory`, `targets.sogo_folders` | SOGo configuration/user preparation, native backups and conflict inspection. |
| `webmail`, `targets.webmail_state` | Ordered webmail orchestration, pinned input/stage receipts and the full import gate for target release. |
| `targets.sogo_import` | Journaled native preference/contact/folder import, preview and readback. |
| `targets.sogo_filters`, `targets.sieve_runtime` | Internal editable-filter import, native preference preservation and Dovecot script readback. |
| `targets.sogo_settings`, `plans.saved_webmail` | Internal native vacation/forwarding import, timezone/prefilter verification and shared saved-proposal checks. |
| `mail`, `core.rsync`, `transfers` | Guarded Maildir append/preview, native rsync progress and shared transfer guards/receipts. |
| `sieve`, `targets.sieve` | Exported filter selection, conflict backups, guarded installation/removal and native compilation. |
| `prefilters` | Inactive forwarding/vacation filter staging, target conflict checks and API request reconciliation. |
| `targets.prefilter_activation` | Internal prefilter activation/readback, copied-Sieve guards and preservation of native handoffs. |
| `senders`, `targets.senders` | Explicit sender ACL merges, security-attribute preservation and live permission verification. |
| `admins`, `targets.admins` | Reviewed administrator ownership/membership import, private credentials and native request reconciliation. |
| `activation`, `accounts` | Ordered account activation, pinned target IDs, partial-phase inventory checks and uncertain-write reconciliation. |
| `quota`, `targets.quota_state` | Native usage recalculation, copied-volume checks, interrupted-attempt receipts and downstream quota guards. |
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
reconciliation before proceeding. Guided resume follows existing receipts and never repeats staging/copying after activation intent.
The API client verifies TLS, rejects redirects, and never retries writes automatically. Command diagnostics are private
by default; callers may display known-safe output with explicit secret redaction.

## Production readiness

The feature implementation is complete within the documented compatibility boundaries, but this release remains pre-alpha.
The final review covered operation ordering, interruption/resume guards, permission scope, private output, shared helpers,
unused paths, naming and packaged resources. It corrected activation-intent checks, source-drift comparison, sender ACL readback,
SOGo native-setting capability checks and stale operational guidance. Repeated logic uses shared implementations.

This was a static review: no automated tests, compilation, linting, package installation or live migration was performed.
Production readiness is therefore **not established**. Qualification still needs representative PostfixAdmin and Plesk pilot
migrations on the supported Linux/Mailcow versions, interruption/recovery exercises, backup restoration checks and real client,
message-content, delivery and authentication verification. Keep the pre-alpha classification until those results are recorded.

- [Compatibility and dependencies](docs/compatibility.md) defines version boundaries and required capabilities.
- [Feature roadmap](docs/features.md) tracks implemented capabilities, planned work, configurable decisions and blockers.
