# Installation and operation

Install Mailcow Migration on the Linux host running the destination Mailcow installation. Run installation and migration
commands as root. The application is an operator-run CLI; no daemon or systemd unit is needed.

The current release is alpha. Ruff checks pass, but installation and complete migration/recovery runs still require validation
on representative servers. Begin with a pilot domain and verified backups.

## 1. Prepare the host

The installer needs Bash, Python 3.8 or newer with `venv` and `ensurepip`, GNU coreutils, and `flock`.
Git is needed if downloading the source with the commands below. Installing the package normally needs access to your
configured Python package index for the setuptools build dependency; there are no third-party runtime packages.

On Debian/Ubuntu, install the client tools if they are missing:

```bash
sudo apt-get update
sudo apt-get install git python3-venv ca-certificates openssh-client rsync dnsutils openssl util-linux
```

Mailcow and Docker Compose must already be installed and working. Arrange SSH authentication to each selected source host,
verify its SSH host key, and allow access to the Mailcow API. Source-specific requirements are listed in
[Compatibility and dependencies](compatibility.md#external-commands-and-access); `check` verifies the configured systems.

## 2. Download and install

For the first installation:

```bash
sudo -i
git clone https://github.com/AlwaysHC/mailcow-migration.git /usr/local/src/mailcow-migration
cd /usr/local/src/mailcow-migration
git log -1 --oneline
bash install.sh
mailcow-migrate --version
```

The clone downloads the current default branch. To deploy a particular reviewed revision, check out that commit before
running `install.sh`. Keep the whole source directory; the installer uses its package metadata and bundled resources.

To select a specific installed interpreter or another installation directory:

```bash
bash install.sh --python /usr/bin/python3.8 --install-dir /opt/mailcow-migration
```

Run `bash install.sh --help` for options. The script stops if the interpreter is older than Python 3.8 or lacks virtual
environment support. For a non-default interpreter, install its matching venv package through your operating system.

The installer creates a new virtual environment at its final location, installs the project with pip, and checks the
installed command's version before activating it. The paths are:

| Path | Purpose |
| --- | --- |
| `/usr/local/src/mailcow-migration` | Source checkout used for installation and updates. |
| `/opt/mailcow-migration/releases/install.*` | Separate installed environments, retained after updates. |
| `/opt/mailcow-migration/current` | Symlink to the active environment. |
| `/opt/mailcow-migration/previous` | Symlink to the environment active before the latest successful update. |
| `/usr/local/bin/mailcow-migrate` | Stable command pointing through `current`; activation of a venv is unnecessary. |
| `/root/mailcow-in-progress` | Default location for configuration, credentials, evidence and recovery journals. |

Installation does not configure sources, contact the Mailcow API or start a migration. An existing unrelated
`/usr/local/bin/mailcow-migrate` is reported as a conflict rather than overwritten. Use the same installation directory on
subsequent runs. If `/usr/local/bin` is missing from the root shell's `PATH`, invoke the command by its full path.

Virtual environments contain absolute interpreter paths: leave each release in its original location. See the
[Python venv documentation](https://docs.python.org/3/library/venv.html) and
[pip's regular installation guidance](https://pip.pypa.io/en/stable/topics/local-project-installs/#regular-installs).

## 3. Configure the migration

From a root shell:

```bash
mailcow-migrate configure --config /root/mailcow-in-progress/migration.json
```

The wizard asks which sources to use: PostfixAdmin, Plesk, or both. Have the following information ready:

- Source SSH hostnames and any SSH configuration file needed for authentication.
- The local Mailcow directory, usually `/opt/mailcow-dockerized`, and its HTTPS API URL.
- The local path where the Mailcow API key will be stored.
- For PostfixAdmin, the actual absolute source mail directory (required; no default) and source-side MySQL option-file path.
- The RoundCube installation directory on each source, containing `config/config.inc.php`; saved once and reused for all its domains.
- Any reviewed migration policy file (optional).

The wizard saves each validated answer immediately in a private draft beside the configuration, for example
`/root/mailcow-in-progress/migration.json.draft.json`. It creates missing parent directories.
If saved configuration exists, it asks you to enter `continue`, `overwrite`, or `q`:

- `continue` resumes a draft at the first unanswered question. If only a completed configuration exists, it proceeds to server setup.
- `overwrite` restarts the questions and saves new answers to the draft. An existing final configuration is replaced only after all answers are complete.
- `q` exits and retains saved answers. Previously saved answers also survive Ctrl+C or interrupted input.

After all answers are complete and valid, the wizard writes the final configuration atomically and removes the draft.
Both files use owner-only permissions. Keep the draft private alongside the configuration.

The same command then checks tools, source database access, Mailcow services and API access. It can prompt for a missing
API key without echoing it and, with approval, create a missing private source MySQL option file. Existing credentials are
retained. After the prerequisite checks pass, applicable shared PostfixAdmin settings and server-administrator choices are
reviewed; applying changes requires explicit approval and may reload shared Postfix services.

If setup pauses or reports blockers, resolve them and rerun the same `configure` command. Keep its configuration and journals;
completed setup work is retained. To change connection details, edit the retained configuration before rerunning setup.
Successful setup does not start a domain migration; continue with step 5.

## 4. Optional readiness checks

`configure` already checks readiness. To repeat those checks later without entering the setup workflow:

```bash
mailcow-migrate check --config /root/mailcow-in-progress/migration.json
```

`check` verifies tools, source database access, Mailcow services and API access. It saves diagnostics and reports blockers
without creating credentials or changing mail-service settings. If setup is incomplete, rerun `configure --config FILE`.

For local configuration validation only, use `mailcow-migrate check-config --config FILE`; it does not contact servers.
The former `prepare` command is now `check`; the former `prepare --guided` workflow is part of `configure`. `migrate` always
provides an interactive domain workflow with confirmations.

## 5. Migrate a pilot domain

Replace `example.org` with the selected domain:

```bash
mailcow-migrate migrate example.org --config /root/mailcow-in-progress/migration.json
```

The guide handles export, planning, isolation, copying, activation, webmail and handoff with operator confirmations.
Review its saved summary and policies, and confirm backups, capacity and stopped writers only after checking them.
DNS changes remain manual. See [Guided operation](../README.md#guided-operation) for the operational boundaries.

`mailcow-migrate --help` shows the main workflow commands. Use `mailcow-migrate migrate --help` for migration options.
Individual operations for detailed review and recovery are listed only with `mailcow-migrate --advanced-help`;
their own `COMMAND --help` remains available directly.

To resume, run the same command with the same configuration and retained batch state. Pausing does not undo completed
operations. Preserve `/root/mailcow-in-progress`, its private credentials, and the recovery paths reported on source hosts.

## Updates

Pause migration work before changing the installed version. Retain the configuration, journals and source recovery state.
From a root shell, update the checkout and rerun the installer:

```bash
cd /usr/local/src/mailcow-migration
git pull --ff-only
git log -1 --oneline
bash install.sh
mailcow-migrate --version
mailcow-migrate check-config --config /root/mailcow-in-progress/migration.json
mailcow-migrate check --config /root/mailcow-in-progress/migration.json
```

For a checkout pinned to a commit, fetch and select the next reviewed commit instead of pulling. Pass the original
`--python` and `--install-dir` options again if they differ from the defaults.

Each successful installation switches `current` to a newly installed environment and records the former one in `previous`.
Installation failures before activation leave the active version in place; the unsuccessful release directory is retained
for inspection. The script serializes installations with a lock and never removes old environments or migration state.
Configuration and state formats must remain compatible with any version selected for an existing batch.

## Roll back the application installation

An application rollback changes the executable used by subsequent commands. It does not reverse mail-server changes or
convert journals written by a newer version. Check version/state compatibility before resuming a batch.

With migration work paused, use the retained previous installation. For the default installation directory, run this block
in a root shell; the subshell releases the installation lock when it exits:

```bash
(
    set -euo pipefail
    exec 9>/opt/mailcow-migration/.install.lock
    flock -n 9
    previous_release=$(readlink -e /opt/mailcow-migration/previous)
    "$previous_release/bin/mailcow-migrate" --version
    pending_link="/opt/mailcow-migration/.rollback-$$"
    ln -s -- "$previous_release" "$pending_link"
    mv -Tf -- "$pending_link" /opt/mailcow-migration/current
    /usr/local/bin/mailcow-migrate --version
)
```

The `previous` link exists after an update; it is absent after a first installation. Adjust the installation paths if you
selected another directory. Keep the release directories until no running process or rollback procedure needs them.
