import re
from typing import Any, Mapping

from .assessment import check
from .domain import active
from .evidence import ExportEvidence
from .forwarding import mailbox_forwardings
from .global_settings import shared_settings_assessment
from .recovery import password_expiration_check, recovery_plan
from .settings_policy import load_policy
from .sieve_evidence import load_sieve_evidence
from .webmail import webmail_assessment
from ..core.errors import MigrationError


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


def administrator_plan(plan: Mapping[str, Any], policy: Mapping[str, Any]) -> Mapping[str, Any]:
    checks, admins = [], []
    source = plan["source"]
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
    return {"checks": checks, "admins": admins}


def policy_plan(plan: Mapping[str, Any], evidence: ExportEvidence, policy: Mapping[str, Any]) -> Mapping[str, Any]:
    forwarding = mailbox_forwardings(plan)
    recovery = recovery_plan(plan)
    administrators = administrator_plan(plan, policy)
    checks = [*forwarding["checks"], *recovery["checks"], *administrators["checks"]]
    admins, source = administrators["admins"], plan["source"]
    scripts = load_sieve_evidence(plan, evidence)
    forwardings = [*forwarding["forwardings"], *scripts.forwardings]
    checks.append(check("fetchmail", "blocked" if source["fetchmail"] else "not-needed",
                        "Fetchmail jobs, including disabled jobs, require a supported native mapping" if source["fetchmail"]
                        else "No fetchmail jobs to migrate"))
    checks.append(password_expiration_check(policy))
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
