import ipaddress
import re
from pathlib import Path
from typing import Any, Mapping, Optional

from .assessment import check
from .domain import active
from .evidence import ExportEvidence
from .forwarding import mailbox_forwardings
from .global_settings import shared_settings_assessment
from .recovery import recovery_plan
from .sieve_evidence import load_sieve_evidence
from .webmail import webmail_assessment
from ..core.errors import ConfigurationError, MigrationError
from ..core.storage import read_json


def load_policy(file: Optional[str]) -> Mapping[str, Any]:
    policy = read_json(Path(file), private=True) if file else {}
    allowed = {"adminNames", "allDomainAdmins", "mergeAdmins", "vacationZeroIntervalDays", "vacationDatesAreUtc",
               "mailcowIp", "senderPermissions", "sourcePasswordExpiration", "vacationTimeZone"}
    if not isinstance(policy, dict) or set(policy) - allowed:
        raise ConfigurationError("Unknown settings policy field; use settings-policy.example.json")
    for name, choices in (("allDomainAdmins", ("review", "batch-only")), ("sourcePasswordExpiration", ("disabled", "enabled", "review"))):
        if name in policy and policy[name] not in choices:
            raise ConfigurationError("{} must be {}".format(name, ", ".join(choices)))
    names = policy.get("adminNames", {})
    if not isinstance(names, dict) or any(not isinstance(value, str) or not value for value in names.values()):
        raise ConfigurationError("adminNames must map source names to nonempty target names")
    merges = policy.get("mergeAdmins", [])
    if not isinstance(merges, list) or any(not isinstance(name, str) for name in merges):
        raise ConfigurationError("mergeAdmins must contain explicitly reviewed target administrator names")
    if "vacationDatesAreUtc" in policy and type(policy["vacationDatesAreUtc"]) is not bool:
        raise ConfigurationError("vacationDatesAreUtc must be boolean")
    interval = policy.get("vacationZeroIntervalDays")
    if interval is not None and (type(interval) is not int or not 1 <= interval <= 30):
        raise ConfigurationError("vacationZeroIntervalDays must be null or a reviewed integer from 1 to 30")
    zone = policy.get("vacationTimeZone", "Europe/Rome")
    if not isinstance(zone, str) or not zone:
        raise ConfigurationError("vacationTimeZone must be a nonempty IANA timezone name")
    if "mailcowIp" in policy:
        try:
            if not isinstance(policy["mailcowIp"], str):
                raise ValueError("Expected IP address text")
            ipaddress.ip_address(policy["mailcowIp"])
        except ValueError as error:
            raise ConfigurationError("mailcowIp must be a fixed IP address") from error
    senders = policy.get("senderPermissions", {})
    if (not isinstance(senders, dict) or any(not isinstance(items, list) or any(not isinstance(item, str) for item in items)
                                           for items in senders.values())):
        raise ConfigurationError("senderPermissions must map users to address lists")
    return {"sourcePasswordExpiration": "disabled", "vacationTimeZone": "Europe/Rome", **policy}


def administrator_name(username: str, policy: Mapping[str, Any]) -> str:
    if username in policy.get("adminNames", {}):
        return policy["adminNames"][username]
    return username.replace("@", ".").lower() if re.fullmatch(r"[A-Za-z0-9_.-]+@[A-Za-z0-9.-]+", username) else username


def sender_requests(plan: Mapping[str, Any], policy: Mapping[str, Any]) -> list:
    users = {row["username"] for row in plan["mailboxes"]}
    addresses = users | {row["address"] for row in plan["aliases"] if not row["address"].startswith("@")}
    permissions = []
    for username, senders in policy.get("senderPermissions", {}).items():
        if username not in users or any(sender not in addresses for sender in senders):
            raise MigrationError("Sender permissions must name real batch users and explicit batch mailbox/alias addresses; no wildcards")
        permissions.append({"username": username, "senders": sorted(set(senders))})
    return permissions


def policy_plan(plan: Mapping[str, Any], evidence: ExportEvidence, policy: Mapping[str, Any]) -> Mapping[str, Any]:
    forwarding = mailbox_forwardings(plan)
    recovery = recovery_plan(plan)
    checks, admins = [*forwarding["checks"], *recovery["checks"]], []
    source = plan["source"]
    scripts = load_sieve_evidence(plan, evidence)
    forwardings = [*forwarding["forwardings"], *scripts.forwardings]
    for row in source["admins"]:
        if active(row.get("superadmin")):
            continue
        bindings = [item for item in source["bindings"] if item["username"] == row["username"]]
        if not bindings:
            continue
        grants = [item for item in bindings if active(item["active"])]
        identifier = "admin:" + row["username"]
        if not grants:
            checks.append(check(identifier, "not-needed", "No active memberships; source records remain archived"))
            continue
        primary = any(item["domain"] == plan["domain"] for item in grants)
        all_domains = any(item["domain"] == "ALL" for item in grants)
        if not primary and all_domains and policy.get("allDomainAdmins") != "batch-only":
            checks.append(check(identifier, "decision", "ALL grant: choose batch-only in policy to grant only this primary domain"))
            continue
        if not primary and not all_domains:
            checks.append(check(identifier, "decision", "Alias-domain-only membership cannot be silently widened to its primary domain"))
            continue
        username = administrator_name(row["username"], policy)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", username) or username == "API":
            checks.append(check(identifier, "decision", "Target requires an administrator name mapping in adminNames"))
            continue
        admins.append({"username": username, "domains": [plan["domain"]], "active": active(row["active"])})
        checks.append(check(identifier, "planned", "Administrator login: {} -> {}; use new admin passwords after activation".format(
            row["username"], username)))
    if len({row["username"].lower() for row in admins}) != len(admins):
        raise MigrationError("Administrator name mappings collide; choose distinct names in adminNames")
    checks.append(check("fetchmail", "blocked" if source["fetchmail"] else "not-needed",
                        "Fetchmail jobs, including disabled jobs, require a supported native mapping" if source["fetchmail"]
                        else "No fetchmail jobs to migrate"))
    expiration = policy["sourcePasswordExpiration"]
    checks.append(check("password-expiration", "not-needed" if expiration == "disabled" else "blocked",
                        "Source password expiration is disabled under the existing policy" if expiration == "disabled" else
                        "Mailcow has no native dated/recurring password expiration; force_pw_update is not equivalent" if expiration == "enabled" else
                        "Confirm whether the source actually enforces password expiration; database dates do not establish this"))
    permissions = sender_requests(plan, policy)
    webmail = webmail_assessment(plan, scripts, policy, evidence.source.connection.kind, forwardings)
    checks.extend(webmail["checks"])
    shared_settings = shared_settings_assessment(plan, evidence, policy)
    checks.extend(shared_settings["checks"])
    checks.extend(check(identifier, "pending", detail) for identifier, detail in (
        ("webmail-data", "RoundCube preservation and SOGo import assessment are not implemented yet"),
        ("cutover-resume", "Isolation, migration journals, DNS handoff and delivery verification are not implemented yet"),
    ))
    return {"checks": checks, "admins": admins, "recovery": recovery["emails"], "forwardings": forwardings,
            "senderPermissions": permissions, "webmail": webmail, "sharedSettings": shared_settings}
