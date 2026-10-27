import getpass
import re
import sys
from pathlib import Path
from shutil import which
from typing import Any, Mapping
from uuid import uuid4

from .configuration import Configuration
from .core.errors import MigrationError
from .core.prompts import WorkflowPaused, confirm_action
from .core.state import Workspace
from .core.storage import (
    json_fingerprint,
    local_path,
    private_directory,
    read_json,
    write_private_json,
    write_private_text,
)
from .plans.server_state import server_directory
from .server_export import export_server, server_source
from .server_settings import run_server_settings
from .sources import SourceConnection, SourceKind, configured_sources
from .sources.php import source_php
from .targets.mailcow import MailcowTarget
from .targets.readiness import inspect_target_host
from .transfers import require_mailcow_host

COMMON_SOURCE_TOOLS = (
    "bash",
    "php",
    "postconf",
    "postfix",
    "postqueue",
    "postmap",
    "postcat",
    "doveconf",
    "doveadm",
    "rsync",
    "find",
    "mktemp",
    "od",
    "gzip",
    "wc",
    "rm",
    "awk",
)
SOURCE_TOOLS = {
    SourceKind.POSTFIXADMIN: (*COMMON_SOURCE_TOOLS, "mysql", "mysqldump", "tar", "scp"),
    SourceKind.PLESK: (*COMMON_SOURCE_TOOLS, "python3", "plesk", "ip"),
}


def source_prerequisites(source: SourceConnection, directory: Path) -> None:
    script = 'for name in "$@"; do if command -v "$name" >/dev/null; then printf "%s\\tavailable\\n" "$name"; '
    script += 'else printf "%s\\tmissing\\n" "$name"; fi; done'
    output = source.ssh.run(
        ("sh", "-c", script, "tools", *SOURCE_TOOLS[source.kind]),
        diagnostic_file=directory / (source.kind.value + "-tools.txt"),
    ).stdout
    rows = tuple(line.split("\t") for line in output.splitlines())
    if len(rows) != len(SOURCE_TOOLS[source.kind]) or any(
        row not in ([name, "available"], [name, "missing"])
        for row, name in zip(rows, SOURCE_TOOLS[source.kind])
    ):
        raise MigrationError("Invalid source prerequisite inventory")
    missing = tuple(row[0] for row in rows if row[1] == "missing")
    if missing:
        raise MigrationError("Source commands missing: " + ", ".join(missing))
    source.ssh.run(
        (
            "php",
            "-r",
            (
                "if (PHP_VERSION_ID < 70300 || !extension_loaded('pdo_mysql')) { "
                "fwrite(STDERR, 'PHP 7.3+ with pdo_mysql is required.\\n'); exit(1); }"
            ),
        ),
        diagnostic_file=directory / (source.kind.value + "-php.txt"),
    )


def source_database(
    source: SourceConnection, directory: Path, *, create: bool = False
) -> Mapping[str, Any]:
    if source.mysql_option_file is None:
        raise MigrationError("PostfixAdmin database option-file path is missing")
    result = source_php(
        source,
        directory,
        "prepare-source-database.php",
        {"file": source.mysql_option_file, "action": "create" if create else "inspect"},
    )
    if (
        type(result.get("exists")) is not bool
        or type(result.get("created")) is not bool
    ):
        raise MigrationError(
            "Invalid source database preparation response; contents omitted"
        )
    if result["exists"]:
        output = source.ssh.run(
            (
                "mysql",
                "--defaults-extra-file=" + source.mysql_option_file,
                "-N",
                "-B",
                "postfix",
                "-e",
                "SELECT COUNT(*) FROM domain",
            ),
            diagnostic_file=directory / "source-database.txt",
        ).stdout.strip()
        if not re.fullmatch(r"[0-9]+", output):
            raise MigrationError(
                "Source database access did not return a valid domain count"
            )
    return result


def run_preparation(configuration: Configuration, *, guided: bool = False) -> int:
    require_mailcow_host()
    workspace = Workspace()
    with workspace.lock():
        directory = private_directory(
            workspace.path(
                "preparation/"
                + json_fingerprint(configuration.document(), canonical=True)
            )
        )
        diagnostics = private_directory(directory / ("attempt-" + uuid4().hex))
        checks = []
        for tool in ("docker", "rsync", "ssh", "scp", "awk", "dig", "openssl"):
            checks.append(
                {
                    "id": "local:" + tool,
                    "status": "passed" if which(tool) else "blocked",
                    "detail": tool,
                }
            )
        target = MailcowTarget(configuration.mailcow)
        try:
            inspect_target_host(target)
        except (MigrationError, OSError) as error:
            checks.append(
                {"id": "target-host", "status": "blocked", "detail": str(error)}
            )
        else:
            checks.append(
                {
                    "id": "target-host",
                    "status": "passed",
                    "detail": "Mailcow services and storage layout are available",
                }
            )
        for source in configured_sources(configuration):
            print(
                f"Checking configured {source.kind.value} source {source.ssh.host}...",
                flush=True,
            )
            try:
                source_prerequisites(source, diagnostics)
                if source.kind is SourceKind.POSTFIXADMIN:
                    database = source_database(source, diagnostics)
                    if not database["exists"]:
                        if not guided:
                            raise MigrationError(
                                "Private source database options are missing; prepare interactively or supply the configured file"
                            )
                        confirm_action(
                            f"Create {source.mysql_option_file} on {source.ssh.host} "
                            "from existing Postfix database credentials; credentials remain on the source.",
                            "create",
                        )
                        source_database(source, diagnostics, create=True)
            except (MigrationError, OSError) as error:
                checks.append(
                    {
                        "id": "source:" + source.kind.value,
                        "status": "blocked",
                        "detail": str(error),
                    }
                )
            else:
                checks.append(
                    {
                        "id": "source:" + source.kind.value,
                        "status": "passed",
                        "detail": "Required source tools and database access checked",
                    }
                )
        key_file = local_path(Path(configuration.mailcow.api_key_file))
        if not key_file.exists() and guided:
            if not sys.stdin.isatty():
                raise MigrationError(
                    "Use an interactive terminal or supply the private API key file; an echoed password prompt is not allowed"
                )
            print(
                "Create a Mailcow read/write API key restricted to this host. It will be saved privately at "
                + str(key_file)
            )
            secret = getpass.getpass(
                "Paste the API key (hidden); Enter pauses: "
            ).strip()
            if not secret:
                raise WorkflowPaused("Paused before API key setup; no key was saved.")
            if any(ord(char) < 33 or ord(char) > 126 for char in secret):
                raise MigrationError("Invalid API key format; no key was saved")
            write_private_text(key_file, secret + "\n")
        try:
            target.api.inventory("domain")
        except (MigrationError, OSError) as error:
            checks.append(
                {"id": "target-api", "status": "blocked", "detail": str(error)}
            )
        else:
            checks.append(
                {
                    "id": "target-api",
                    "status": "passed",
                    "detail": "Target API inventory is accessible",
                }
            )
        report = {
            "version": 1,
            "kind": "server-prerequisites",
            "configuration": configuration.document(),
            "checks": checks,
            "diagnostics": str(diagnostics),
            "complete": all(row["status"] == "passed" for row in checks),
        }
        write_private_json(directory / "checks.json", report, replace=True)
        for row in checks:
            print("[{}] {}: {}".format(row["status"], row["id"], row["detail"]))
        if not report["complete"] or not guided:
            return int(not report["complete"])
        if configuration.postfixadmin is not None:
            source = server_source(configuration)
            state_directory = server_directory(source, configuration)
            state_file = state_directory / "state.json"
            if state_file.exists():
                state = read_json(state_file, private=True)
                if not isinstance(state, dict) or not isinstance(
                    state.get("export"), str
                ):
                    raise MigrationError(
                        "Invalid shared-server state; retain it before continuing"
                    )
                exported = Path(state["export"])
                if state.get("complete") is True:
                    run_server_settings(configuration, exported, action="check")
                    print(
                        "Previously completed shared preparation passed fresh verification."
                    )
                    return 0
            else:
                selection = state_directory / "inventory.json"
                if selection.exists():
                    inventory = read_json(selection, private=True)
                    if not isinstance(inventory, dict) or not isinstance(
                        inventory.get("export"), str
                    ):
                        raise MigrationError(
                            "Invalid shared-server inventory; retain it before continuing"
                        )
                    exported = Path(inventory["export"])
                else:
                    confirm_action(
                        "Export the source's shared settings privately for review; mail service is unchanged.",
                        "export",
                    )
                    exported = export_server(configuration)
            run_server_settings(configuration, exported, action="preview")
            if run_server_settings(configuration, exported, action="review"):
                raise WorkflowPaused(
                    "Shared settings review is incomplete; saved answers remain available."
                )
            confirm_action(
                "Apply the reviewed shared settings and approved server administrator choices. Shared Postfix may reload.",
                "apply",
            )
            run_server_settings(configuration, exported, action="apply", apply=True)
        print(
            "Server prerequisites and applicable shared settings are prepared. Domain migration remains a separate guided operation."
        )
    return 0
