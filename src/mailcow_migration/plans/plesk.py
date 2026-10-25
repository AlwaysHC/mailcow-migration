import base64
import hashlib
import math
import re
import secrets
from typing import Any, Mapping

from .domain import MIB, SAFE_INTEGER, TABLES, build_plan, require_acyclic, valid_domain
from ..configuration import MigrationConfiguration
from ..core.errors import MigrationError


def plesk_address(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[a-z0-9_+.-]+@[^@\s]+", value)) and valid_domain(value.split("@")[1])


def plesk_password(row: Mapping[str, Any]) -> str:
    credential = row["credential"]
    value = credential["value"]
    if not isinstance(value, str) or not value or re.search(r"[\r\n\0]", value):
        raise MigrationError("Invalid source password: " + row["username"])
    if not credential["encrypted"]:
        salt = secrets.token_bytes(16)
        digest = hashlib.sha256(value.encode("utf-8") + salt).digest()
        return "{SSHA256}" + base64.b64encode(digest + salt).decode("ascii")
    password = re.sub(r"^\{(?:CRYPT|MD5-CRYPT|SHA256-CRYPT|SHA512-CRYPT|BLF-CRYPT)\}", "", value)
    schemes = (
        (r"\$1\$[^$]{1,8}\$[./a-zA-Z0-9]{22}", "MD5-CRYPT"),
        (r"\$5\$(?:rounds=[0-9]+\$)?[^$]{1,16}\$[./a-zA-Z0-9]{43}", "SHA256-CRYPT"),
        (r"\$6\$(?:rounds=[0-9]+\$)?[^$]{1,16}\$[./a-zA-Z0-9]{86}", "SHA512-CRYPT"),
        (r"\$2[aby]\$[0-9][0-9]\$[./a-zA-Z0-9]{53}", "BLF-CRYPT"),
    )
    scheme = next((name for pattern, name in schemes if re.fullmatch(pattern, password)), None)
    if scheme is None:
        raise MigrationError("Unsupported or truncated password hash: " + row["username"])
    return "{" + scheme + "}" + password


def plesk_plan(snapshot: Mapping[str, Any], settings: MigrationConfiguration) -> Mapping[str, Any]:
    domain, accounts, parameters = snapshot["domain"], snapshot["accounts"], snapshot["parameters"]
    blockers, names = list(snapshot["blockers"]), set()
    mailboxes = [row for row in accounts if row["mailbox"]]
    for row in accounts:
        username = row["username"]
        if not plesk_address(username) or username != row["localPart"] + "@" + domain or row["localPart"] in (".", ".."):
            raise MigrationError("Invalid Plesk mailbox address")
        if username in names:
            raise MigrationError("Duplicate Plesk address: " + username)
        names.add(username)
        if type(row["quota"]) is not int or not 0 <= row["quota"] <= SAFE_INTEGER:
            raise MigrationError("Invalid Plesk quota: " + username)
        if row["autoresponder"]:
            blockers.append(username + ": convert its Plesk autoresponder before migration")
        if row["forwarding"] and (not row["destinations"] or any(not plesk_address(item.lower()) for item in row["destinations"])):
            blockers.append(username + ": invalid or missing forwarding destinations")
        if row["forwarding"] and any(item.lower() == username for item in row["destinations"]):
            blockers.append(username + ": self-referencing forwarding requires review")
        if not row["delivery"] and not row["forwarding"] and row["mailbox"]:
            blockers.append(username + ": delivery disabled without forwarding")
        if row["script"] and re.search(r"\binclude\b", row["script"], re.I):
            blockers.append(username + ": external Sieve includes require migration")
        if row["script"] and row["forwarding"]:
            blockers.append(username + ": forwarding with personal Sieve needs a reviewed ordering")
    aliases = []
    for row in accounts:
        targets = [row["username"]] if row["mailbox"] else [item.lower() for item in row["destinations"]] if row["forwarding"] else []
        if not targets:
            blockers.append(row["username"] + ": address has neither a mailbox nor forwarding")
        aliases.append({"address": row["username"], "goto": ",".join(targets), "domain": domain, "active": 1})
        aliases.extend({"address": local + "@" + domain, "goto": row["username"], "domain": domain, "active": 1}
                       for local in row["aliases"])
    if parameters.get("nonexist_mail") == "catch":
        target = parameters.get("catch_addr", "").lower()
        if not plesk_address(target):
            blockers.append("Invalid Plesk catch-all destination")
        else:
            aliases.append({"address": "@" + domain, "goto": target, "domain": domain, "active": 1})
    elif parameters.get("nonexist_mail") != "reject":
        blockers.append("Unsupported Plesk unknown-recipient policy: {}; review before migration".format(
            parameters.get("nonexist_mail", "missing")))
    seen = set()
    for row in aliases:
        if row["address"] in seen:
            blockers.append("Duplicate source alias: " + row["address"])
            break
        seen.add(row["address"])
    domain_aliases = []
    for row in snapshot["aliasDomains"]:
        if row["mail"] not in ("true", "false", "1", "0") or not re.fullmatch(r"[0-9]+", row["status"]):
            raise MigrationError("Unsupported Plesk domain-alias schema; expected mail and status fields")
        if row["mail"] in ("true", "1"):
            domain_aliases.append({"alias_domain": row["name"], "target_domain": domain, "active": int(int(row["status"]) == 0)})
    rows = {table: [] for table in TABLES}
    rows.update({
        "domain": [{"domain": domain, "description": "Imported from Plesk: " + domain, "aliases": 0,
                    "mailboxes": 0, "active": int(snapshot["active"])}],
        "mailbox": [{"username": row["username"], "local_part": row["localPart"], "domain": domain, "name": row["name"],
                     "quota": row["quota"], "active": 1, "credential": row["credential"], "email_other": row["recoveryEmail"],
                     **{key: row[key] for key in ("quarantine_notification", "quarantine_category") if key in row}}
                    for row in mailboxes],
        "alias": aliases, "alias_domain": domain_aliases,
    })
    quota = max([1, *(math.ceil(row["quota"] / MIB) for row in mailboxes)])
    built = build_plan(rows, domain, quota, settings, password_for=plesk_password)
    routes = {row["username"]: [item.lower() for item in row["destinations"]] if row["forwarding"] else [] for row in accounts}
    routes.update({row["address"]: row["goto"].split(",") for row in aliases if row["address"] != row["goto"]})
    require_acyclic(routes, "Plesk forwarding")
    return {**built, "blockers": list(dict.fromkeys([*built["blockers"], *blockers])), "source": {
        **built["source"], "kind": "plesk",
        "mailboxes": [{key: value for key, value in row.items() if key != "credential"} for row in built["source"]["mailboxes"]],
    }, "notes": [
        "Mailbox forwarding is installed as personal Sieve; source keep-copy settings are preserved.",
        "Roundcube contacts and supported preferences are imported into SOGo before restoring Mailcow access.",
        "Mailcow spam/antivirus policies apply. Plesk websites and panel users are outside this command.",
        "The script blocks source mail access/delivery before copying. Complete DNS/SMTP handoff and retain the Plesk VM and state.",
    ]}
