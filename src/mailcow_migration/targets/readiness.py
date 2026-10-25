import re
from pathlib import Path
from shutil import which
from typing import Any, Mapping

from .mailcow import MailcowTarget
from ..core.errors import MigrationError
from ..core.storage import read_text
from ..plans.assessment import check
from ..plans.domain import active, address_domain
from ..plans.policy import administrator_name

DOMAIN_FIELDS = {"aliases": "max_num_aliases_for_domain", "mailboxes": "max_num_mboxes_for_domain",
                 "defquota": "def_quota_for_mbox", "maxquota": "max_quota_for_mbox", "quota": "max_quota_for_domain"}


def inspect_target_host(target: MailcowTarget) -> Mapping[str, Any]:
    missing = [tool for tool in ("docker", "rsync", "ssh", "scp", "awk", "dig") if which(tool) is None]
    if missing:
        raise MigrationError("Required target commands are unavailable: " + ", ".join(missing))
    configuration = read_text(Path(target.settings.directory) / "mailcow.conf")
    if not re.search(r"^MAILDIR_SUB=Maildir\s*$", configuration, re.M):
        raise MigrationError("Migration requires Mailcow MAILDIR_SUB=Maildir")
    version = target.compose(("version",)).stdout.strip()
    services = {}
    for service in ("dovecot-mailcow", "postfix-mailcow"):
        identifier = target.compose(("ps", "-q", service)).stdout.strip()
        if not re.fullmatch(r"[a-f0-9]+", identifier):
            raise MigrationError("Mailcow service is not running: " + service)
        services[service] = identifier
    return {"compose": version, "services": services, "maildirSub": "Maildir"}


def target_assessment(
    plan: Mapping[str, Any], target: MailcowTarget, account_policy: Mapping[str, Any], policy: Mapping[str, Any],
) -> Mapping[str, Any]:
    host = inspect_target_host(target)
    inventory = {}
    for kind in ("domain", "mailbox", "alias-domain", "alias", "bcc"):
        rows = target.api.request("get/{}/all".format(kind))
        if any(not isinstance(row, dict) for row in rows):
            raise MigrationError("Invalid target {} inventory; contents omitted".format(kind))
        inventory[kind] = rows
    belongs = lambda value: address_domain(str(value).strip().lower()) in plan["domains"]
    domains = [row for row in inventory["domain"] if (row.get("domain_name") or row.get("domain")) in plan["domains"]]
    aliases = [row for row in inventory["alias-domain"] if row.get("alias_domain") in plan["domains"]]
    mailboxes = [row for row in inventory["mailbox"] if (row.get("domain") or address_domain(row.get("username", ""))) in plan["domains"]]
    candidate = len(domains) == 1 and (domains[0].get("domain_name") or domains[0].get("domain")) == plan["domain"] and not aliases and not mailboxes
    reasons, checks = [], []
    if candidate:
        for field in ("mboxes_in_domain", "aliases_in_domain", "bytes_total", "msgs_total"):
            value = domains[0].get(field)
            if not (type(value) is int and value == 0 or isinstance(value, str) and value == "0"):
                reasons.append(field + " is nonzero or not reported by Mailcow")
        reasons.extend("Alias domain {} points into this batch".format(row["alias_domain"])
                       for row in inventory["alias-domain"] if belongs(row.get("target_domain")))
        reasons.extend("Alias {} belongs to or forwards into this batch".format(row["address"])
                       for row in inventory["alias"] if belongs(row.get("address")) or any(belongs(item) for item in str(row.get("goto")).split(",")))
        reasons.extend("BCC {} refers to this batch".format(row["local_dest"])
                       for row in inventory["bcc"] if belongs(row.get("local_dest")) or belongs(row.get("bcc_dest")))
    reusable = candidate and not reasons
    conflict = bool(domains or aliases or mailboxes) and not reusable
    if conflict:
        checks.append(check("target-accounts", "blocked",
                            "Existing Mailcow objects conflict with this plan. "
                            "Locate the original batch and journal if these came from an earlier run."))
    else:
        checks.append(check("target-accounts", "verified", "Existing empty primary domain can be reused after confirmation" if reusable
                            else "No conflicting primary domains, alias domains or mailboxes"))
    mailbox_names = {row["username"] for row in plan["mailboxes"]}
    if any(row.get("address") in mailbox_names for row in inventory["alias"]):
        checks.append(check("target-native-routes", "blocked",
                            "A non-native alias exists for a planned mailbox; reconcile the alias before account import"))
    changes = []
    if reusable:
        for key, value in plan["domainPayload"].items():
            if key == "domain":
                continue
            previous = domains[0].get(DOMAIN_FIELDS.get(key, key))
            if key in ("defquota", "maxquota", "quota") and previous is not None:
                previous = float(previous) / 1048576
            changes.append({"field": key, "before": previous, "after": value})
    if account_policy["admins"]:
        administrators = target.api.request("get/domain-admin/all")
        if any(not isinstance(row, dict) for row in administrators):
            raise MigrationError("Invalid target administrator inventory; contents omitted")
        for row in account_policy["admins"]:
            found = [item for item in administrators if item.get("username") == row["username"]]
            renamed = any(not active(item.get("superadmin")) and item["username"] != row["username"]
                          and administrator_name(item["username"], policy) == row["username"] for item in plan["source"]["admins"])
            if len(found) > 1:
                checks.append(check("target-admin:" + row["username"], "blocked", "Duplicate target administrator names"))
            elif found and row["username"] not in policy.get("mergeAdmins", []) and (
                renamed or any(domain not in (found[0].get("selected_domains") or []) for domain in row["domains"])
            ):
                checks.append(check("target-admin:" + row["username"], "decision",
                                    "Confirm account ownership in mergeAdmins before adding this batch; "
                                    "preserve existing security settings and memberships"))
    return {"host": host, "checks": checks, "domainAction": "conflict" if conflict else "reuse-empty" if reusable else "create",
            "domainChanges": changes, "conflicts": {
                "domains": [{"domain": row.get("domain_name") or row.get("domain"), "active": row.get("active")} for row in domains],
                "aliasDomains": [{key: row.get(key) for key in ("alias_domain", "target_domain", "active")} for row in aliases],
                "mailboxes": [{key: row.get(key) for key in ("username", "active")} for row in mailboxes], "reasons": reasons,
            }}
