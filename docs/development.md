# Development environment

Use a local virtual environment and an editable installation of this repository. Python source edits then take effect on
the next invocation without reinstalling the application. See [pip's editable installation guide](https://pip.pypa.io/en/stable/topics/local-project-installs/#editable-installs).

Windows supports editing, linting and inspecting CLI help. Configuration creation and operational migration work require
Linux and the services described in [Compatibility and dependencies](compatibility.md). Use a separate Linux Mailcow
environment with representative source systems for integration work.

## Prerequisites

- Git.
- Python 3.8 or newer, with virtual environment support.
- An editor such as VS Code.

The application has no third-party runtime dependencies. Ruff is a development tool. The commands below pin Ruff to
`0.17.3`, the version used for the existing lint cleanup, so its rule set does not change unexpectedly.

Run all commands from this repository's root. If you already have a checkout, skip the clone and enter that directory.
Open the `Mailcow-Migration` repository itself in your editor; the separate legacy `Mailcow` directory is not required.

## Windows: PowerShell

```powershell
git clone https://github.com/AlwaysHC/mailcow-migration.git
cd mailcow-migration

py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install --editable . "ruff==0.17.3"

.\.venv\Scripts\python.exe --version
.\.venv\Scripts\python.exe -m mailcow_migration --version
.\.venv\Scripts\python.exe -m mailcow_migration --help
```

`py -3` selects an installed Python 3 interpreter. To create the environment with an installed Python 3.8 interpreter,
use `py -3.8 -m venv .venv` instead. Choose the interpreter before creating the environment.

These commands invoke the environment's Python directly, so PowerShell activation and execution-policy changes are
unnecessary. If the Python launcher is unavailable, use the full path to your installed `python.exe` for the venv command.

## Linux: Bash

On Debian/Ubuntu, install Git and `python3-venv` if needed. For a non-default interpreter, install its corresponding venv
package. Create the development environment as your normal user:

```bash
git clone https://github.com/AlwaysHC/mailcow-migration.git
cd mailcow-migration

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install --editable . "ruff==0.17.3"

.venv/bin/python --version
.venv/bin/python -m mailcow_migration --version
.venv/bin/python -m mailcow_migration --help
```

Select `python3.8` instead of `python3` when creating an environment specifically for the minimum supported interpreter.
You can optionally activate the environment with `source .venv/bin/activate`, then use `python` and `mailcow-migrate`
directly. No production installation or global command symlink is needed for development.

## VS Code

1. Open the repository root as a folder.
2. Install the Microsoft Python extension and the [Ruff extension](https://docs.astral.sh/ruff/editors/setup/#vs-code).
3. Run **Python: Select Interpreter** and choose `.venv\Scripts\python.exe` on Windows or `.venv/bin/python` on Linux.
4. Select Ruff as the Python formatter if you want editor formatting. The repository's `pyproject.toml` provides its settings.

The terminal lint commands below use the pinned Ruff installation. An editor extension may bundle another Ruff version;
use the terminal result when checking the project's lint status.

For a local debug session that only displays CLI help, this optional `.vscode/launch.json` configuration uses the selected
interpreter and runs the package as a module:

```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "Mailcow Migration: help",
      "type": "debugpy",
      "request": "launch",
      "module": "mailcow_migration",
      "args": ["--help"],
      "cwd": "${workspaceFolder}",
      "console": "integratedTerminal"
    }
  ]
}
```

## Formatting and linting

On Windows:

```powershell
.\.venv\Scripts\python.exe -m ruff format .
.\.venv\Scripts\python.exe -m ruff check .
```

On Linux:

```bash
.venv/bin/python -m ruff format .
.venv/bin/python -m ruff check .
```

`format` modifies files. Use `format --check .` to inspect formatting without changing it. Review the diff after formatting
or applying lint fixes. The ignored local `Lint.bat` and `Lint.txt` files are optional conveniences, not setup requirements.

Ruff targets Python 3.8 through `pyproject.toml`. This project uses annotations that work at runtime on Python 3.8, such as
`Optional[str]` and `Tuple[str, ...]`. `from __future__ import annotations` is banned. `FA100` is disabled because that rule
requires the banned import; other compatibility checks remain enabled.

Keep new syntax, standard-library calls and dependency versions compatible with Python 3.8. Running Ruff with a newer
interpreter does not execute the application on Python 3.8 or prove its runtime compatibility.

## Source layout and updates

Application code is under `src/mailcow_migration`; the module entry point is `python -m mailcow_migration` and the installed
command is `mailcow-migrate`. Bundled Python, PHP and shell helpers are under `src/mailcow_migration/resources`.
See [Package layout](../README.md#package-layout) for the main components.

After pulling changes, the editable installation picks up Python source edits. If package metadata, entry points or build
requirements change, repeat the editable install command with the same environment's Python. Recreate the environment
when changing its base interpreter; virtual environments should not be moved between paths or machines.

`.venv`, Python caches and build outputs are ignored by Git. Keep real configuration, API keys, database exports and
migration journals outside the source checkout; generic JSON files are not automatically ignored. Use `examples/` for
sample configuration and retain the private working state on the Linux host.

## Integration work and deployment

Run configuration and migration commands on a Linux Mailcow host with root privileges and suitable source access.
Use the explicit path to the development environment's interpreter when running as root, so the command does not
accidentally use a globally installed release. Confirm which interpreter and application version are active first.

Use dedicated pilot domains and the same retained journals when exercising interruption and resume. CLI help and lint
checks do not validate mail delivery, authentication, recovery or source/target integrations.

For installation of a reviewed checkout as a separate deployed release, follow the [installation guide](installation.md).
`install.sh` performs a regular installation into its own environment; subsequent source edits affect the editable
development environment, while the deployed copy changes only when installed again.
