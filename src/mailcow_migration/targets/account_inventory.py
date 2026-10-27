from typing import Any, Mapping

from ..plans.assessment import check
from ..plans.domain import address_domain, number
from .mailcow import MailcowApi

DOMAIN_FIELDS = {
    "aliases": "max_num_aliases_for_domain",
    "mailboxes": "max_num_mboxes_for_domain",
    "defquota": "def_quota_for_mbox",
    "maxquota": "max_quota_for_mbox",
    "quota": "max_quota_for_domain",
}


def domain_value(row: Mapping[str, Any], key: str) -> Any:
    value = row.get(DOMAIN_FIELDS.get(key, key))
    return (
        number(value) / 1048576
        if value is not None and key in ("defquota", "maxquota", "quota")
        else value
    )


def account_inventory(api: MailcowApi) -> Mapping[str, list]:
    return {
        kind: api.inventory(kind)
        for kind in ("domain", "mailbox", "alias-domain", "alias", "bcc")
    }


def native_route_conflicts(plan: Mapping[str, Any], aliases: list) -> list:
    names = {row["address"] for row in plan["aliases"] if row["mailbox"]}
    return [row["address"] for row in aliases if row.get("address") in names]


def account_conflicts(
    plan: Mapping[str, Any], inventory: Mapping[str, list]
) -> Mapping[str, Any]:
    belongs = lambda value: (
        address_domain(str(value).strip().lower()) in plan["domains"]
    )
    domains = [
        row
        for row in inventory["domain"]
        if (row.get("domain_name") or row.get("domain")) in plan["domains"]
    ]
    aliases = [
        row
        for row in inventory["alias-domain"]
        if row.get("alias_domain") in plan["domains"]
    ]
    mailboxes = [
        row
        for row in inventory["mailbox"]
        if (row.get("domain") or address_domain(row.get("username") or ""))
        in plan["domains"]
    ]
    candidate = (
        len(domains) == 1
        and (domains[0].get("domain_name") or domains[0].get("domain"))
        == plan["domain"]
        and not aliases
        and not mailboxes
    )
    reasons, checks = [], []
    if candidate:
        for field in (
            "mboxes_in_domain",
            "aliases_in_domain",
            "bytes_total",
            "msgs_total",
        ):
            value = domains[0].get(field)
            if not (
                type(value) is int
                and value == 0
                or isinstance(value, str)
                and value == "0"
            ):
                reasons.append(field + " is nonzero or not reported by Mailcow")
        reasons.extend(
            "Alias domain {} points into this batch".format(row["alias_domain"])
            for row in inventory["alias-domain"]
            if belongs(row.get("target_domain"))
        )
        reasons.extend(
            "Alias {} belongs to or forwards into this batch".format(row["address"])
            for row in inventory["alias"]
            if belongs(row.get("address"))
            or any(belongs(item) for item in str(row.get("goto")).split(","))
        )
        reasons.extend(
            "BCC {} refers to this batch".format(row["local_dest"])
            for row in inventory["bcc"]
            if belongs(row.get("local_dest")) or belongs(row.get("bcc_dest"))
        )
    reusable = candidate and not reasons
    conflict = bool(domains or aliases or mailboxes) and not reusable
    if conflict:
        checks.append(
            check(
                "target-accounts",
                "blocked",
                "Existing Mailcow objects conflict with this plan. "
                "Locate the original batch and journal if these came from an earlier run.",
            )
        )
    else:
        checks.append(
            check(
                "target-accounts",
                "verified",
                "Existing empty primary domain can be reused after confirmation"
                if reusable
                else "No conflicting primary domains, alias domains or mailboxes",
            )
        )
    if native_route_conflicts(plan, inventory["alias"]):
        checks.append(
            check(
                "target-native-routes",
                "blocked",
                "A non-native alias exists for a planned mailbox; reconcile the alias before account import",
            )
        )
    changes = (
        [
            {"field": key, "before": domain_value(domains[0], key), "after": value}
            for key, value in plan["domainPayload"].items()
            if key != "domain"
        ]
        if reusable
        else []
    )
    return {
        "checks": checks,
        "domainAction": "conflict"
        if conflict
        else "reuse-empty"
        if reusable
        else "create",
        "domainChanges": changes,
        "existingDomain": domains[0] if reusable else None,
        "conflicts": {
            "domains": [
                {
                    "domain": row.get("domain_name") or row.get("domain"),
                    "active": row.get("active"),
                }
                for row in domains
            ],
            "aliasDomains": [
                {
                    key: row.get(key)
                    for key in ("alias_domain", "target_domain", "active")
                }
                for row in aliases
            ],
            "mailboxes": [
                {key: row.get(key) for key in ("username", "active")}
                for row in mailboxes
            ],
            "reasons": reasons,
        },
    }
