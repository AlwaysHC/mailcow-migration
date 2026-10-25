# Mailcow Migration

A Python CLI for preparing mail-domain migrations from PostfixAdmin and Plesk to an existing Mailcow installation.

**Status: pre-alpha (`0.1.0.dev0`).** Configuration, source inspection, private exports and read-only account planning are implemented.
Account creation, message transfer and cutover are not implemented yet. This development snapshot has not been validated end to end.

| Command | Available functionality |
| --- | --- |
| `configure` | Interactive setup for one or both sources and the Mailcow target. |
| `check-config` | Local configuration validation. |
| `inspect` | Source inventories, Maildir checks and authoritative DNS checks. |
| `export` | Private account/configuration snapshots with completion manifests and file hashes. |
| `plan` | Account mappings, quota calculations, policy decisions and initial target conflict checks. |

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
Both source planners use this resolver when constructing mailbox payloads. Account migration is not implemented yet.

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
not modified. RoundCube export, shared-server preparation and migration-resume support remain planned work.

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
the configured headroom/minimums are not used in that mode. Target checks still run. Plesk always uses measured quotas.

Read-only target checks cover local prerequisites, Docker Compose, running Dovecot/Postfix services,
`MAILDIR_SUB=Maildir`, and API domain/mailbox/alias/BCC inventories. An existing primary domain is reusable only when empty,
with no relevant alias-domain, alias or BCC dependencies. Its proposed setting changes are recorded. Conflicting existing
objects remain blocked; an earlier batch must be recovered using its original plan and journal, never adopted by name alone.

The configured policy supplies administrator names and batch-only ALL grants, explicitly reviewed administrator merges,
sender permissions, and the source password-expiration decision. Recovery email/phone, fetchmail, inactive Sieve, and mailbox
forwarding checks report unsupported mappings. Plesk forwarding retains its keep-copy choice for later personal Sieve import.
No account, mail-service or policy changes are applied. Sizing uses a temporary source file listing that its helper removes.

The new directory contains `plan.json`, `account-policy.json`, `policy.json`, `assessment.json`, `summary.txt`, `users.txt`,
`domains.json`, measurement diagnostics when present, and a completion manifest. Directories use mode 700 and files mode 600.
The shared operation lock covers planning. Existing outputs are never overwritten; interrupted runs retain an incomplete manifest.
Exit status 1 reports blockers or unresolved administrator decisions; 0 means account planning completed without those findings.

**A completed plan is not migration approval.** `assessment.json` always records `migration_ready: false` in this version.
Full native Sieve/vacation/SOGo/RoundCube assessment, shared-server and sender-restriction assessment, storage/transfer readiness,
isolation, DNS handoff, delivery verification and journal-aware resume remain pending. These are explicitly reported as pending.
Source inventory rechecks do not prove all account settings stayed unchanged after export. Plans record a digest for
their assessment. Planning does not resume an existing migration batch; retain its saved state and recovery evidence.

## Package layout

| Module | Responsibility |
| --- | --- |
| `cli`, `wizard`, `configuration` | Entry point, interactive setup, immutable settings, and shared validation. |
| `inspection`, `core.dns`, `sources.inventory` | Read-only source inventories, authoritative DNS agreement, and inspection reports. |
| `exporting`, `sources.export` | Private source exports, inventory rechecks, completion manifests and file hashes. |
| `planning`, `plans`, `sources.sql`, `sources.usage` | Account mapping, policy decisions, export validation and measured quotas. |
| `targets.readiness` | Read-only target prerequisite checks, account conflicts and empty-domain reuse assessment. |
| `core.commands`, `core.ssh` | Command execution, timeouts, private diagnostics, and SSH/SCP/rsync arguments. |
| `core.storage`, `core.state` | Private JSON files, a shared operation lock, identity checks, and mutation journals. |
| `sources` | Source connections, inspection and domain account/configuration export adapters. |
| `targets.mailcow` | HTTPS API requests, safe read retries, and Docker Compose commands. |
| `resources` | Packaged source inspection/export helpers and the existing policy example. |

CLI errors use exit status 1, configuration/usage errors 2, interruption 130, and shared-lock contention 75.
Library callers must hold the workspace lock and verify batch identity before journaled writes. Pending writes require
reconciliation before retrying; endpoint-specific reconciliation and migration resume remain planned work.
The API client verifies TLS, rejects redirects, and never retries writes automatically. Command diagnostics are private
by default; callers may display known-safe output with explicit secret redaction.

- [Compatibility and dependencies](docs/compatibility.md) defines version boundaries and required capabilities.
- [Feature roadmap](docs/features.md) tracks implemented capabilities, planned work, configurable decisions and blockers.
