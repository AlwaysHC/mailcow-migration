import ipaddress
import re
from datetime import datetime, timezone
from typing import Any, Mapping

from ..core.postfix import postfix_settings
from ..core.storage import json_fingerprint
from ..sources import SourceKind
from .assessment import check
from .domain import valid_domain
from .evidence import ExportEvidence
from .postfix import (
    MAP_NAMES,
    PostfixEvidence,
    load_postfix_evidence,
    source_policy_evidence,
)
from .senders import sender_permission_check


def global_inventory(raw_evidence: PostfixEvidence) -> list:
    evidence = source_policy_evidence(raw_evidence)
    context = json_fingerprint(
        {
            "postfix": evidence.postfix,
            "services": evidence.services,
            "dovecot": evidence.dovecot,
        }
    )
    configuration = (evidence.postfix or "") + "\n" + (evidence.services or "")
    inventory = {}

    def item(
        name: str, key: str, action: str, scope: str, missing: bool = False
    ) -> Mapping[str, Any]:
        return {
            "id": json_fingerprint(
                {"context": context, "map": name, "key": key, "action": action}
            ),
            "map": name,
            "key": key,
            "action": action,
            "scope": scope,
            "missing": missing,
        }

    for name in MAP_NAMES:
        text = evidence.maps.get(name)
        if not isinstance(text, str):
            row = item(name, "missing inventory", "", "unknown", True)
            inventory[row["id"]] = row
            continue
        references = re.findall(
            r"([a-z]+):/etc/postfix/" + name + r"(?=[\s,}]|$)", configuration
        )
        reference = bool(references) and all(
            value in ("hash", "btree", "lmdb", "cdb", "texthash")
            for value in references
        )
        caller = (
            r"\btransport_maps\s*=[^\n]*"
            if name == "transport"
            else (
                r"\bcheck_sender_access\s+"
                if name == "sender_access"
                else r"\bcheck_client_access\s+"
            )
        )
        expected_use = (
            re.search(
                caller + r"(?:proxy:)?[a-z]+:/etc/postfix/" + name + r"(?=[\s,}]|$)",
                configuration,
            )
            is not None
        )
        for line in re.split(r"\r?\n", text):
            if not line.strip() or line.strip().startswith("#"):
                continue
            match = re.fullmatch(r"(\S+)\s+(.+)", line.strip())
            key, action = (match[1], match[2]) if match else (line.strip(), "")
            supported = (
                match is not None
                and not line[0].isspace()
                and reference
                and expected_use
                and not re.search(r"[{}]", key)
            )
            scope = (
                "unknown"
                if not supported
                else "shared"
                if name == "rbl_override"
                else "lookup"
            )
            row = item(name, key, action, scope)
            inventory[row["id"]] = row
    return list(inventory.values())


def classify_map(
    row: Mapping[str, Any], evidence: PostfixEvidence
) -> Mapping[str, Any]:
    name, key, action = row["map"], row["key"], row["action"]
    referenced = "/etc/postfix/" + name in (evidence.postfix or "") + "\n" + (
        evidence.services or ""
    )

    def result(status: str, reason: str, **extra: Any) -> Mapping[str, Any]:
        return {**row, "status": status, "reason": reason, **extra}

    if row["missing"]:
        return result(
            "blocker" if referenced else "adapted",
            f"Referenced {name} map is missing from the export; refresh the source export."
            if referenced
            else "No source reference to this map",
        )
    if not referenced:
        return result(
            "adapted",
            "Source map is not referenced by the exported Postfix configuration",
        )
    if row["scope"] == "unknown":
        return result(
            "blocker" if name == "transport" else "unsupported",
            f"Cannot interpret active {name} lookup semantics: {key}",
        )
    if name == "rbl_override":
        try:
            ipaddress.ip_address(key)
            is_ip = True
        except ValueError:
            is_ip = False
        if is_ip and re.fullmatch("OK", action, re.IGNORECASE):
            return result(
                "automatic",
                "Adapt to Mailcow's global Postscreen IP whitelist: bypass Postscreen including RBL checks for this IP. "
                "Rspamd filtering and SMTP relay/recipient restrictions remain in place; this is not identical to source OK.",
                kind="postscreen-whitelist",
                adaptation=True,
            )
        return result(
            "unsupported",
            "Only individual IPv4/IPv6 addresses with an OK action have a supported whitelist adaptation",
        )
    if name == "sender_access":
        if key == "autoreply.nextware.it" and re.fullmatch(
            r"FILTER\s+smtp:", action, re.IGNORECASE
        ):
            return result(
                "adapted",
                "Source vacation reinjection is replaced by the domain migration's native Sieve vacations",
            )
        if re.fullmatch(
            r"[a-z0-9_.+-]+@[a-z0-9.-]+", key, re.IGNORECASE | re.ASCII
        ) and re.fullmatch("REJECT", action, re.IGNORECASE):
            return result(
                "automatic",
                "Install an exact envelope-sender rejection for unauthenticated external SMTP",
                kind="sender",
            )
        return result(
            "unsupported",
            "No implemented equivalent for this sender action; the original rule is retained in this report",
        )
    if key == "autoreply.nextware.it" and action == "vacation:":
        return result(
            "adapted",
            "Source vacation transport is replaced by the domain migration's native Sieve vacations",
        )
    if re.fullmatch(r"@[^@]+", key):
        return result(
            "unsupported",
            "An @domain key is not a domain-wide Postfix transport lookup; it is not broadened into a new route",
        )
    route = re.fullmatch(
        r"(smtp|relay):(\[[a-z0-9_.:-]+\](?::[0-9]+)?)",
        action,
        re.IGNORECASE | re.ASCII,
    )
    domain_key = key[1:] if key.startswith(".") else key
    if route is None or not valid_domain(domain_key):
        return result(
            "blocker",
            f"Unsupported recipient route {key} -> {action}; an equivalent route is needed to preserve delivery",
        )
    services = re.sub(r"\n[ \t]+", " ", evidence.services or "")
    service = next(
        (
            line
            for line in re.split(r"\r?\n", services)
            if line.startswith((route[1] + " ", route[1] + "\t"))
        ),
        None,
    )
    if service is None or not re.fullmatch(
        re.escape(route[1]) + r"\s+unix(?:\s+\S+){5}\s+smtp\s*", service
    ):
        return result(
            "blocker",
            f"Source {route[1]} transport has custom or missing service options; cannot assume SMTP equivalence",
        )
    return result(
        "automatic",
        "Create and verify a native Mailcow destination transport",
        kind="transport",
        destination=key.lower(),
        nexthop=route[2],
    )


def plan_global_settings(raw_evidence: PostfixEvidence) -> list:
    evidence = source_policy_evidence(raw_evidence)
    config = postfix_settings(evidence.postfix)
    rows = [classify_map(row, evidence) for row in global_inventory(evidence)]

    def setting(key: str, status: str, reason: str, **extra: Any) -> Mapping[str, Any]:
        return {
            "id": json_fingerprint(
                {
                    "key": key,
                    **({"value": config[key]} if key in config else {}),
                    "config": config,
                }
            ),
            "map": "postfix",
            "key": key,
            "action": config.get(key) or "",
            "scope": "shared",
            "status": status,
            "reason": reason,
            **extra,
        }

    if not evidence.postfix or not evidence.services:
        rows.append(
            setting(
                "source-evidence",
                "blocker",
                "Exported Postfix configuration is missing",
            )
        )
    if config.get("relayhost"):
        supported = (
            re.fullmatch(
                r"\[[a-z0-9_.:-]+\](?::[0-9]+)?",
                config["relayhost"],
                re.IGNORECASE | re.ASCII,
            )
            is not None
        )
        rows.append(
            setting(
                "relayhost",
                "automatic" if supported else "blocker",
                "Preserve the default outgoing relay in Mailcow's persistent extra.cf"
                if supported
                else "Unsupported default relay syntax",
                kind="relayhost",
                nexthop=config["relayhost"],
            )
        )
    for key in (
        "sender_dependent_relayhost_maps",
        "sender_dependent_default_transport_maps",
        "smtp_sender_dependent_authentication",
        "recipient_canonical_maps",
        "sender_canonical_maps",
        "canonical_maps",
        "relocated_maps",
        "smtp_generic_maps",
    ):
        if config.get(key) and config[key] != "no":
            rows.append(
                setting(
                    key,
                    "blocker",
                    f"Source {key} affects routing/addresses and has no implemented mapping",
                )
            )
    transport_maps = config.get("transport_maps")
    if (
        transport_maps
        and transport_maps != "hash:/etc/postfix/transport"
        and not re.fullmatch(
            r"(btree|lmdb|cdb|texthash):/etc/postfix/transport", transport_maps
        )
    ):
        rows.append(
            setting(
                "transport_maps",
                "blocker",
                "The complete source transport lookup chain is not supported",
            )
        )
    rows.extend(
        {
            "id": json_fingerprint({"admin": admin}),
            "map": "global-administrators",
            "key": admin["username"],
            "action": "",
            "scope": "shared",
            "status": "unsupported",
            "reason": "Server administrator identity needs a target password and privilege mapping",
        }
        for admin in evidence.administrators
    )
    for key in (
        "header_checks",
        "body_checks",
        "smtp_header_checks",
        "smtpd_milters",
        "non_smtpd_milters",
        "smtpd_client_restrictions",
        "smtpd_helo_restrictions",
        "smtpd_data_restrictions",
        "smtpd_sender_restrictions",
        "smtpd_recipient_restrictions",
        "mynetworks",
    ):
        if config.get(key):
            rows.append(
                setting(
                    key,
                    "unsupported",
                    "Source policy chain is not copied wholesale; supported sender rules migrate separately "
                    "and Mailcow retains its own spam/security policy",
                )
            )
    if config.get("content_filter"):
        rows.append(
            setting(
                "content_filter",
                "blocker",
                "Custom source content_filter delivery path has no implemented equivalent",
            )
        )
    return rows


def rule_relation(row: Mapping[str, Any], plan: Mapping[str, Any]) -> Mapping[str, str]:
    if row["scope"] == "unknown":
        return {
            "kind": "unknown",
            "reason": "Lookup semantics or source evidence need global review",
        }
    if row["map"] == "default-sieve":
        return {
            "kind": "shared",
            "reason": "Fallback filtering for users without personal filters",
        }
    if row["scope"] == "shared":
        return {"kind": "shared", "reason": "Server policy applies across domains"}
    key = row["key"].lower()
    if key == "*":
        return {"kind": "shared", "reason": "Wildcard rule"}
    domain_key = key[1:] if key.startswith(".") else key
    mailbox_key = re.fullmatch(r"[^@]+@[^@]+", key) is not None and valid_domain(
        key.split("@")[1]
    )
    if not valid_domain(domain_key) and not mailbox_key:
        return {"kind": "unknown", "reason": "Unsupported lookup key; review globally"}
    domains = plan.get("domains", [plan["domain"]])
    local = [row["username"] for row in plan.get("mailboxes", [])] + [
        row["address"] for row in plan.get("aliases", [])
    ]
    destinations = [
        target
        for row in plan.get("aliases", [])
        for target in row["payload"].get("goto", "").split(",")
        if target
    ]
    destinations.extend(
        row["bcc_dest"] for row in plan.get("bcc", []) if row["bcc_dest"]
    )

    def matches(address: str) -> bool:
        value = address.lower()
        domain = value.split("@")[1] if "@" in value else value
        if mailbox_key:
            return value == key or re.sub(r"\+[^@]+(?=@)", "", value, count=1) == key
        return (
            domain.endswith("." + domain_key)
            if key.startswith(".")
            else domain == key or domain.endswith("." + key)
        )

    candidates = (
        [*domains, *local, *destinations]
        if row["map"] == "transport"
        else [*domains, *local]
    )
    matched = next((value for value in candidates if matches(value)), None)
    if matched is not None:
        return {
            "kind": "direct",
            "reason": "Matches {} as a {}".format(
                matched, "recipient" if row["map"] == "transport" else "sender"
            ),
        }
    if mailbox_key and any(key.endswith("@" + domain) for domain in domains):
        return {
            "kind": "direct",
            "reason": "Address-specific rule within " + key.split("@")[1],
        }
    return {
        "kind": "shared",
        "reason": "Other destination: potentially used by outgoing mail from any domain"
        if row["map"] == "transport"
        else "Other sender: potentially used by incoming mail to any domain",
    }


def shared_settings_assessment(
    plan: Mapping[str, Any], exported: ExportEvidence, policy: Mapping[str, Any]
) -> Mapping[str, Any]:
    kind = exported.source.connection.kind
    evidence = (
        load_postfix_evidence(plan, exported)
        if kind is SourceKind.POSTFIXADMIN
        else PostfixEvidence(None, None, None, {}, ())
    )
    inventory = (
        plan_global_settings(evidence) if kind is SourceKind.POSTFIXADMIN else []
    )
    rows = [
        {
            **row,
            "relation": rule_relation(row, plan),
            "canExclude": row["status"] not in ("adapted", "blocker")
            and not row.get("missing")
            and row["key"] != "source-evidence",
        }
        for row in inventory
    ]
    counts = {
        status: sum(row["status"] == status for row in rows)
        for status in ("automatic", "adapted", "unsupported", "blocker")
    }
    if kind is SourceKind.POSTFIXADMIN:
        statuses = {
            "blocker": "blocked",
            "unsupported": "not-migrated",
            "automatic": "planned",
        }
        checks = [
            check(
                "global-rule:{}:{}".format(row["map"], row["key"]),
                statuses[row["status"]],
                row["relation"]["reason"]
                + "; inspect the source value and mapping in shared-settings-plan.json",
            )
            for row in rows
            if row["status"] != "adapted" and row["relation"]["kind"] == "direct"
        ]
        checks.append(
            check(
                "global-baseline",
                "blocked"
                if counts["blocker"]
                else "required"
                if counts["automatic"] or counts["unsupported"]
                else "not-needed",
                "{} blockers, {} supported mappings, {} unsupported settings, {} informational settings. "
                "Full server inventory and reasons are retained privately in shared-settings-plan.json.".format(
                    counts["blocker"],
                    counts["automatic"],
                    counts["unsupported"],
                    counts["adapted"],
                ),
            )
        )
        checks.append(
            check(
                "shared-settings-application",
                "required",
                "Review/apply shared settings through server-settings; matching source/target receipts and live target readback "
                "are required before account writes, source freezing and activation",
            )
        )
    else:
        checks = [
            check(
                "global-baseline",
                "not-needed",
                "Shared PostfixAdmin settings are outside the Plesk mail-domain workflow",
            )
        ]
    sender_policy = sender_permission_check(evidence, policy)
    checks.append(
        {key: value for key, value in sender_policy.items() if key != "services"}
    )
    return {
        "version": 1,
        "kind": "source-shared-settings-plan",
        "sourceKind": kind.value,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "export_id": exported.manifest["export_id"],
        "target_verified": False,
        "selection_recorded": False,
        "inventory_fingerprint": json_fingerprint(inventory),
        "inventory": rows,
        "counts": counts,
        "senderPolicy": sender_policy,
        "checks": checks,
    }
