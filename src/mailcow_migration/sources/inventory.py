import re
from dataclasses import dataclass
from typing import Any, Mapping, Tuple

from . import SourceConnection, SourceKind
from .plesk import read_plesk
from ..configuration import mail_root
from ..core.dns import normalized_ip
from ..core.domain import domain_name
from ..core.errors import ConfigurationError, MigrationError
from ..resources import read_resource


@dataclass(frozen=True)
class DomainInventory:
    present: bool
    active: bool
    primary: str
    mail_root: str
    mailboxes: Tuple[str, ...]
    aliases: Tuple[str, ...]

    @classmethod
    def from_document(cls, document: Any) -> "DomainInventory":
        if (not isinstance(document, dict) or type(document.get("present")) is not bool
                or type(document.get("active")) is not bool or not isinstance(document.get("mailboxes"), list)
                or not isinstance(document.get("aliases"), list)):
            raise MigrationError("Invalid source domain inventory")
        mailboxes = document["mailboxes"]
        if any(not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_+.-]+", name) or name in (".", "..") for name in mailboxes):
            raise MigrationError("Invalid source mailbox inventory")
        if len(set(mailboxes)) != len(mailboxes):
            raise MigrationError("Duplicate source mailboxes")
        try:
            return cls(
                document["present"], document["active"], domain_name(document.get("primary")), mail_root(document.get("mailRoot")),
                tuple(sorted(mailboxes)), tuple(sorted(domain_name(name) for name in document["aliases"])),
            )
        except ConfigurationError as error:
            raise MigrationError("Invalid source domain or mail-root inventory") from error


@dataclass(frozen=True)
class SourceInspection:
    connection: SourceConnection
    inventory: DomainInventory
    ips: Tuple[str, ...]
    domain_directory: bool
    missing_maildirs: Tuple[str, ...]


def inspect_postfixadmin(connection: SourceConnection, domain: str) -> Mapping[str, Any]:
    domain_name(domain)
    if connection.mysql_option_file is None or connection.mail_root is None:
        raise MigrationError("PostfixAdmin inspection requires a MySQL credentials file and mail root")
    sql = (
        f"SELECT 'domain', domain, active FROM domain WHERE domain='{domain}' UNION ALL "
        f"SELECT 'primary', target_domain, active FROM alias_domain WHERE alias_domain='{domain}' UNION ALL "
        f"SELECT 'mailbox', username, active FROM mailbox WHERE domain='{domain}' UNION ALL "
        f"SELECT 'alias', alias_domain, active FROM alias_domain WHERE target_domain='{domain}'"
    )
    output = connection.ssh.run((
        "mysql", "--defaults-extra-file=" + connection.mysql_option_file, "--batch", "--skip-column-names", "postfix", "-e", sql,
    )).stdout
    rows = tuple(tuple(line.split("\t")) for line in output.splitlines())
    if any(
        len(row) != 3 or row[0] not in ("domain", "primary", "mailbox", "alias") or not row[1] or row[2] not in ("0", "1")
        for row in rows
    ):
        raise MigrationError("Invalid PostfixAdmin source inventory")
    domains = tuple(row for row in rows if row[0] == "domain")
    primaries = tuple(row for row in rows if row[0] == "primary")
    if len(domains) > 1 or len(primaries) > 1 or domains and primaries:
        raise MigrationError("Ambiguous PostfixAdmin primary/alias domain")
    mailboxes = tuple(row[1] for row in rows if row[0] == "mailbox")
    if any(not name.endswith("@" + domain) for name in mailboxes):
        raise MigrationError("PostfixAdmin mailbox belongs to another domain")
    return {
        "present": bool(domains), "active": bool(domains) and domains[0][2] == "1",
        "primary": primaries[0][1] if primaries else domain, "mailRoot": connection.mail_root,
        "mailboxes": [name[:-len(domain) - 1] for name in mailboxes],
        "aliases": [row[1] for row in rows if row[0] == "alias" and row[2] == "1"],
    }


def inspect_plesk(connection: SourceConnection, domain: str) -> Any:
    return read_plesk(connection, "inspect", domain)


def inspect_source(connection: SourceConnection, domain: str) -> SourceInspection:
    domain_name(domain)
    inspect = inspect_plesk if connection.kind is SourceKind.PLESK else inspect_postfixadmin
    inventory = DomainInventory.from_document(inspect(connection, domain))
    subdir = "Maildir" if connection.kind is SourceKind.PLESK else ""
    output = connection.ssh.run(
        ("bash", "-s", "--", inventory.mail_root, domain, subdir, *inventory.mailboxes),
        input_text=read_resource("source-directories.sh"), timeout=60,
    ).stdout
    rows = tuple(tuple(line.split("\t")) for line in output.splitlines())
    if (len(rows) != len(inventory.mailboxes) + 2 or len(rows[0]) != 2 or rows[0][0] != "addresses"
            or len(rows[1]) != 2 or rows[1][0] != "directory" or rows[1][1] not in ("0", "1")):
        raise MigrationError("Invalid source directory/address inventory")
    ips = tuple(sorted({normalized_ip(address) for address in rows[0][1].split()}))
    if not ips or any(
        len(row) != 3 or row[0] != "mailbox" or row[1] != name or row[2] not in ("0", "1")
        for name, row in zip(inventory.mailboxes, rows[2:])
    ):
        raise MigrationError("Invalid source mailbox directory inventory")
    return SourceInspection(connection, inventory, ips, rows[1][1] == "1", tuple(row[1] for row in rows[2:] if row[2] != "1"))
