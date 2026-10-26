from pathlib import Path
from typing import Any, Mapping, Tuple

from . import SourceConnection
from .inventory import DomainInventory, SourceInspection
from .plesk import read_plesk
from ..configuration import WORK_ROOT
from ..core.errors import MigrationError
from ..core.storage import read_text, write_private_json, write_private_text
from ..resources import read_resource


def postfixadmin_locations(identifier: str) -> Mapping[str, str]:
    return {
        "scripts": "{}/scripts/python-{}".format(WORK_ROOT, identifier),
        "export": "{}/exports/python-{}".format(WORK_ROOT, identifier),
    }


def postfixadmin_export(source: SourceInspection, domain: str, directory: Path, identifier: str) -> Tuple[str, ...]:
    return postfixadmin_snapshot(source.connection, source.inventory.mail_root, domain, directory, identifier)


def postfixadmin_snapshot(
    connection: SourceConnection, root: str, scope: str, directory: Path, identifier: str,
) -> Tuple[str, ...]:
    if connection.mysql_option_file is None:
        raise MigrationError("PostfixAdmin export requires a MySQL credentials file")
    locations = postfixadmin_locations(identifier)
    helpers = tuple(directory / "helpers" / name for name in ("export-domain.sh", "dump-progress.awk"))
    for helper in helpers:
        write_private_text(helper, read_resource(helper.name))
    connection.ssh.run(
        ("bash", "-s", "--", WORK_ROOT, identifier), input_text=read_resource("prepare-export.sh"),
        diagnostic_file=directory / "prepare-diagnostics.txt",
    )
    connection.ssh.copy_to(helpers, locations["scripts"], directory / "upload-diagnostics.txt")
    connection.ssh.run(
        ("env", "MAILCOW_WORK_ROOT=" + WORK_ROOT, "bash", locations["scripts"] + "/export-domain.sh", scope, locations["export"],
         connection.mysql_option_file, "postfix", root),
        timeout=3600, diagnostic_file=directory / "export-diagnostics.txt",
    )
    connection.ssh.copy_from(locations["export"], directory / "data", directory / "download-diagnostics.txt")
    return ()


def validate_postfixadmin_export(directory: Path, domain: str, root: str) -> None:
    server = domain == "--server"
    expected = {"COMPLETE": "complete", "scope.txt": "server" if server else "domain",
                **({} if server else {"domain.txt": domain, "mail-root.txt": root})}
    for name, value in expected.items():
        if read_text(directory / name, private=True).strip() != value:
            raise MigrationError("Downloaded PostfixAdmin export is incomplete or has a different domain/mail root")
    required = (
        "source-settings.sql", "postfix-version.txt", "dovecot-version.txt", "postfix-effective.conf", "postfix-services.conf",
        "dovecot-effective.conf", "postfix-queue.txt", "server-config.tgz", "row-counts.tsv", "dumped-row-count.txt",
        *(() if server else ("config/default.sieve", "config/inactive-sieve.files")),
    )
    if any(not (directory / name).is_file() for name in required) or not server and not (directory / "sieve").is_dir():
        raise MigrationError("Downloaded PostfixAdmin export is missing required evidence")
    if not (directory / "source-settings.sql").stat().st_size or not (directory / "server-config.tgz").stat().st_size:
        raise MigrationError("Downloaded PostfixAdmin dump or configuration archive is empty")


def validate_plesk_snapshot(snapshot: Any, source: SourceInspection, domain: str) -> Tuple[str, ...]:
    if (not isinstance(snapshot, dict) or type(snapshot.get("version")) is not int or snapshot["version"] != 1
            or snapshot.get("domain") != domain or not isinstance(snapshot.get("parameters"), dict)
            or any(not isinstance(snapshot.get(key), list) for key in (
                "accounts", "aliasDomains", "autoresponders", "responseForwards", "responseAttachments", "mailingLists", "blockers",
            ))):
        raise MigrationError("Invalid Plesk snapshot or source domain identity")
    accounts, aliases = snapshot["accounts"], snapshot["aliasDomains"]
    if (any(not isinstance(row, dict) or type(row.get("mailbox")) is not bool for row in accounts)
            or any(not isinstance(row, dict) or row.get("mail") not in ("true", "false", "1", "0")
                   or not isinstance(row.get("status"), str) or not row["status"].isascii() or not row["status"].isdigit()
                   for row in aliases)
            or any(not isinstance(blocker, str) for blocker in snapshot["blockers"])):
        raise MigrationError("Invalid Plesk account, alias or blocker inventory")
    inventory = DomainInventory.from_document({
        "present": True, "active": snapshot.get("active"), "primary": snapshot["domain"], "mailRoot": snapshot.get("mailRoot"),
        "mailboxes": [row.get("localPart") for row in accounts if row["mailbox"]],
        "aliases": [row.get("name") for row in aliases if row["mail"] in ("true", "1") and int(row["status"]) == 0],
    })
    if inventory != source.inventory:
        raise MigrationError("Plesk snapshot differs from the inspected source inventory; keep the export and review source changes")
    return tuple(snapshot["blockers"])


def plesk_export(source: SourceInspection, domain: str, directory: Path) -> Tuple[str, ...]:
    snapshot = read_plesk(source.connection, "export", domain, directory / "export-diagnostics.txt")
    write_private_json(directory / "data" / "source.json", snapshot)
    return validate_plesk_snapshot(snapshot, source, domain)
