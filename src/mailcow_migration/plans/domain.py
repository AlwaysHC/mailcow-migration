import re
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from ..configuration import MigrationConfiguration
from ..core.domain import domain_name
from ..core.errors import ConfigurationError, MigrationError
from ..core.storage import read_text
from .passwords import password_scheme

MIB = 1048576
QUOTA_LIMIT = 1_000_000_000
SAFE_INTEGER = 9007199254740991
TABLES = (
    "domain",
    "mailbox",
    "alias",
    "alias_domain",
    "vacation",
    "admin",
    "domain_admins",
    "fetchmail",
)


def number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError) as error:
        raise MigrationError(
            "Invalid numeric source field; contents omitted"
        ) from error


def active(value: Any) -> int:
    return int(number(value) == 1)


def address_domain(value: str) -> str:
    return value.rsplit("@", 1)[-1]


def valid_domain(value: Any) -> bool:
    try:
        domain_name(value)
        return True
    except ConfigurationError:
        return False


def address(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(re.fullmatch(r"[^\s@/,;\\]+@[^\s@/,;\\]+", value))
        and valid_domain(address_domain(value))
    )


def destinations(value: Any) -> list:
    return [part.strip().lower() for part in str(value).split(",") if part.strip()]


def read_map(directory: Optional[Path], name: str) -> list:
    if directory is None or not (directory / name).exists():
        return []
    result = []
    for line in read_text(directory / name, private=True).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"(\S+)\s+(.+?)\s*", line)
        if match is None:
            raise MigrationError(f"Unsupported map line in {name}")
        result.append({"source": match[1].lower(), "target": match[2]})
    return result


def require_acyclic(routes: Mapping[str, list], label: str) -> None:
    visiting, visited = set(), set()
    for start in routes:
        stack = [(start, False)]
        while stack:
            name, leaving = stack.pop()
            if leaving:
                visiting.remove(name)
                visited.add(name)
            elif name in visiting:
                raise MigrationError(f"{label} cycle involving {name}")
            elif name not in visited:
                visiting.add(name)
                stack.append((name, True))
                stack.extend(
                    (target, False) for target in reversed(routes.get(name, []))
                )


def postfixadmin_password(row: Mapping[str, Any]) -> str:
    password = row.get("password")
    if not isinstance(password, str):
        raise MigrationError("Missing source password for {}".format(row["username"]))
    password = "{MD5-CRYPT}" + password if password.startswith("$1$") else password
    password_scheme(password, row["username"], ("MD5-CRYPT",))
    return password


def build_plan(
    rows: Mapping[str, Any],
    domain: str,
    quota: int,
    settings: MigrationConfiguration,
    config: Optional[Path] = None,
    password_for: Callable[[Mapping[str, Any]], str] = postfixadmin_password,
) -> Mapping[str, Any]:
    domain_name(domain)
    if type(quota) is not int or not 1 <= quota <= QUOTA_LIMIT:
        raise MigrationError("Invalid mailbox quota")
    for table in TABLES:
        if not isinstance(rows.get(table), list) or any(
            not isinstance(row, dict) for row in rows[table]
        ):
            raise MigrationError(f"Missing or invalid source table {table}")
    primary = [row for row in rows["domain"] if row["domain"] == domain]
    if len(primary) != 1 or any(
        row["alias_domain"] == domain for row in rows["alias_domain"]
    ):
        raise MigrationError("Select a unique primary domain, not an alias domain")
    names = dict.fromkeys([domain])
    while True:
        previous = len(names)
        for row in rows["alias_domain"]:
            if row["target_domain"] in names:
                names[row["alias_domain"]] = None
        if previous == len(names):
            break
    blockers, notes = [], []
    alias_domains = [
        row for row in rows["alias_domain"] if row["alias_domain"] in names
    ]
    for row in alias_domains:
        if not valid_domain(row["alias_domain"]):
            blockers.append("Invalid alias domain")
        if row["target_domain"] != domain:
            blockers.append("Review chained alias domain: " + row["alias_domain"])
        if any(item["domain"] == row["alias_domain"] for item in rows["domain"]):
            blockers.append("Primary/alias overlap: " + row["alias_domain"])
    mailboxes = [row for row in rows["mailbox"] if row["domain"] in names]
    if any(row["domain"] != domain for row in mailboxes):
        blockers.append("Real mailbox under an alias domain needs explicit mapping")
    mailbox_names = {row["username"] for row in mailboxes}
    if len(mailbox_names) != len(mailboxes):
        raise MigrationError("Duplicate mailbox")
    mailbox_plans = []
    for row in mailboxes:
        username = row["username"]
        local_part = username.rsplit("@", 1)[0]
        if (
            not address(username)
            or username != str(row["local_part"]).lower() + "@" + row["domain"]
            or local_part in (".", "..")
        ):
            raise MigrationError("Invalid or inconsistent mailbox name")
        if local_part != row["local_part"]:
            notes.append(
                f"Use canonical username casing for {username}; verify source Maildir casing"
            )
        if number(row["quota"]) > quota * MIB:
            blockers.append("Chosen quota is below source limit: " + username)
        password = password_for(row)
        mailbox_plans.append(
            {
                "username": username,
                "active": active(row["active"]),
                "payload": {
                    "domain": row["domain"],
                    "local_part": local_part,
                    "name": row["name"],
                    "password": password,
                    "password2": password,
                    "active": 0,
                    "quota": quota,
                    "force_pw_update": 0,
                    **settings.quarantine_attributes(row),
                },
            }
        )
    static = read_map(config, "virtual")
    null_targets = {
        "devnull",
        *(row["source"] for row in static if row["target"] == "devnull"),
    }
    selected = [row for row in rows["alias"] if row["domain"] in names]
    for row in static:
        if address_domain(row["source"]) not in names:
            continue
        mapped = {
            "address": row["source"],
            "goto": row["target"],
            "domain": address_domain(row["source"]),
            "active": 1,
        }
        index = next(
            (
                index
                for index, item in enumerate(selected)
                if item["address"] == row["source"]
            ),
            len(selected),
        )
        if index == len(selected):
            selected.append(mapped)
        else:
            selected[index] = mapped
    for row in mailboxes:
        if not any(item["address"] == row["username"] for item in selected):
            selected.append(
                {
                    "address": row["username"],
                    "goto": row["username"],
                    "domain": domain,
                    "active": row["active"],
                }
            )
    all_domains = {row["domain"] for row in rows["domain"]} | {
        row["alias_domain"] for row in rows["alias_domain"]
    }
    all_local = {row["address"] for row in rows["alias"]} | {
        row["username"] for row in rows["mailbox"]
    }
    aliases = []
    for row in selected:
        name = row["address"]
        if not (address(name) or name.startswith("@") and valid_domain(name[1:])):
            raise MigrationError("Unsupported alias address")
        targets = [
            item
            for item in destinations(row["goto"])
            if not item.endswith("@autoreply.nextware.it")
        ]
        non_null = [item for item in targets if item not in null_targets]
        if not targets:
            blockers.append("Alias has only vacation targets: " + name)
        for target in non_null:
            if not address(target):
                blockers.append("Invalid target for " + name)
            if (
                address_domain(target) in all_domains
                and target not in all_local
                and "@" + address_domain(target) not in all_local
            ):
                notes.append(
                    f"Verify local alias-domain/catch-all resolution: {name} -> {target}"
                )
        aliases.append(
            {
                "address": name,
                "active": active(row["active"]),
                "mailbox": name in mailbox_names,
                "payload": {
                    "address": name,
                    "active": 0,
                    **({"goto": ",".join(non_null)} if non_null else {"goto_null": 1}),
                },
            }
        )
    require_acyclic(
        {
            row["address"]: [
                item
                for item in destinations(row["payload"].get("goto", ""))
                if item != row["address"]
            ]
            for row in aliases
        },
        "Alias",
    )
    source_mailboxes = {row["username"]: row for row in rows["mailbox"]}
    for row in aliases:
        for target in destinations(row["payload"].get("goto", "")):
            if (
                row["active"]
                and target in source_mailboxes
                and not active(source_mailboxes[target]["active"])
            ):
                notes.append(
                    "Active alias targets an inactive mailbox: {} -> {}".format(
                        row["address"], target
                    )
                )
    bcc = []
    for row in read_map(config, "copia_posta_uscita"):
        if address_domain(row["source"]) not in names:
            continue
        if not address(row["target"]):
            blockers.append("Unsupported BCC destination: " + row["source"])
        notes.append(
            "Verify BCC routing and remove duplicate old rule: {} -> {}".format(
                row["source"], row["target"]
            )
        )
        bcc.append(
            {
                "local_dest": row["source"].lstrip("@"),
                "bcc_dest": row["target"],
                "type": "sender",
                "active": 0,
            }
        )
    vacations = [row for row in rows["vacation"] if row["domain"] in names]
    bindings = [
        row
        for row in rows["domain_admins"]
        if row["domain"] in names or row["domain"] == "ALL"
    ]
    admins = [
        {key: value for key, value in row.items() if key not in ("password", "token")}
        for row in rows["admin"]
        if any(item["username"] == row["username"] for item in bindings)
        or active(row.get("superadmin"))
    ]
    if number(primary[0].get("backupmx")):
        blockers.append("Backup-MX domain requires a separate transport mapping")
    if not active(primary[0]["active"]):
        notes.append(
            "Primary is disabled: test alias-domain delivery and mailbox login semantics"
        )
    notes.extend(
        (
            "Verify selected quota against logical mailbox usage; a finite quota changes source unlimited accounts",
            "Review domain alias/mailbox count limits and recovery/password-expiry fields against the source records",
            "Complete batch filters, sender permissions, webmail data and domain routing; assess dependencies on the global review",
        )
    )
    if vacations:
        notes.append(
            f"Recreate and test {len(vacations)} dated vacation records; preserve inactive states"
        )
    if bindings:
        notes.append(
            f"Create/merge {len(bindings)} domain admin bindings; use new admin passwords"
        )
    if any(row["domain"] == "ALL" for row in bindings):
        notes.append(
            "Review administrative ALL-domain grants without granting future batches implicitly"
        )
    return {
        "version": 1,
        "domain": domain,
        "domains": list(names),
        "quota": quota,
        "blockers": blockers,
        "notes": list(dict.fromkeys(notes)),
        "domainActive": active(primary[0]["active"]),
        "domainPayload": {
            "domain": domain,
            "description": primary[0]["description"],
            "active": 1,
            "backupmx": 0,
            "relay_all_recipients": 0,
            "aliases": max(number(primary[0]["aliases"]), len(aliases) + 10, 1000),
            "mailboxes": max(
                number(primary[0]["mailboxes"]), len(mailboxes) + 10, 1000
            ),
            "defquota": quota,
            "maxquota": quota,
            "quota": quota * max(len(mailboxes), 1),
        },
        "mailboxes": mailbox_plans,
        "aliases": aliases,
        "aliasDomains": alias_domains,
        "bcc": bcc,
        "source": {
            "primary": primary[0],
            "mailboxes": mailboxes,
            "vacations": vacations,
            "admins": admins,
            "bindings": bindings,
            "fetchmail": [row for row in rows["fetchmail"] if row["domain"] in names],
        },
    }
