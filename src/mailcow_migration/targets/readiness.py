import re
from pathlib import Path
from shutil import which
from typing import Any, Mapping

from .account_inventory import account_conflicts, account_inventory
from .mailcow import MailcowTarget
from .passwords import check_password_support
from ..core.errors import MigrationError
from ..core.storage import read_text
from ..plans.assessment import check
from ..plans.domain import active
from ..plans.policy import administrator_name

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
    assessment = account_conflicts(plan, account_inventory(target.api))
    checks = list(assessment["checks"])
    schemes = check_password_support(plan, target)
    checks.append(check("password-schemes", "supported" if schemes else "not-needed",
                        "Target Dovecot supports " + ", ".join(schemes) if schemes else "No mailbox passwords to transfer"))
    if account_policy["admins"]:
        administrators = target.api.inventory("domain-admin")
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
    return {"host": host, **{key: value for key, value in assessment.items() if key != "existingDomain"}, "checks": checks}
