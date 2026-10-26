import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .domain import active, valid_domain
from .catalog import register_batch
from ..configuration import Configuration
from ..core.errors import MigrationError
from ..core.storage import json_fingerprint, local_path, read_json
from ..sources import SourceConnection, configured_sources
from ..sources.inventory import DomainInventory


@dataclass(frozen=True)
class SavedPlan:
    directory: Path
    plan: Mapping[str, Any] = field(repr=False)
    assessment: Mapping[str, Any] = field(repr=False)
    fingerprint: str
    source: SourceConnection

    @property
    def blocking_findings(self) -> str:
        return "\n".join("{}: {}".format(row["id"], row["detail"]) for row in self.assessment["checks"]
                         if row["status"] in ("blocked", "decision"))


def validate_plan_inventory(plan: Mapping[str, Any], inventory: DomainInventory) -> None:
    mailboxes = sorted(row["payload"]["local_part"] for row in plan["mailboxes"] if row["payload"]["domain"] == plan["domain"])
    aliases = sorted(row["alias_domain"] for row in plan["aliasDomains"] if row["target_domain"] == plan["domain"] and active(row["active"]))
    if (not inventory.present or inventory.primary != plan["domain"] or tuple(mailboxes) != inventory.mailboxes
            or tuple(aliases) != inventory.aliases or bool(plan["domainActive"]) != inventory.active):
        raise MigrationError("Source domain/mailbox inventory differs from the plan; retain the batch and reconcile before continuing")


def validate_saved_plan(plan: Any) -> None:
    if (not isinstance(plan, dict) or type(plan.get("version")) is not int or plan["version"] != 1
            or not valid_domain(plan.get("domain")) or not isinstance(plan.get("blockers"), list)
            or any(not isinstance(item, str) for item in plan["blockers"])):
        raise MigrationError("Unsupported plan version or domain")
    if plan["blockers"]:
        raise MigrationError("Plan is blocked:\n" + "\n".join(plan["blockers"]))
    domains = plan.get("domains")
    if (not isinstance(domains, list) or any(not valid_domain(item) for item in domains)
            or len(set(domains)) != len(domains) or plan["domain"] not in domains):
        raise MigrationError("Invalid batch domains")
    for name in ("mailboxes", "aliases", "aliasDomains", "bcc"):
        if not isinstance(plan.get(name), list) or any(not isinstance(row, dict) for row in plan[name]):
            raise MigrationError("Missing or invalid account plan inventory: " + name)
    if not isinstance(plan.get("source"), dict) or not isinstance(plan.get("domainPayload"), dict):
        raise MigrationError("Missing source or domain payload")
    try:
        if plan["domainPayload"]["domain"] != plan["domain"] or plan["domainPayload"]["active"] != 1:
            raise MigrationError("Invalid domain staging payload")
        for row in plan["mailboxes"]:
            payload = row["payload"]
            local = payload["local_part"]
            if payload["domain"] != plan["domain"] or row["username"] != local + "@" + plan["domain"]:
                raise MigrationError("Mailbox is outside the primary domain")
            if not re.fullmatch(r"[^\s@/,;\\]+", local) or local in (".", ".."):
                raise MigrationError("Unsafe mailbox path component")
            if payload["active"] != 0:
                raise MigrationError("Mailbox staging requires inactive accounts")
        if any(row["payload"]["active"] != 0 for row in plan["aliases"]) or any(row["active"] != 0 for row in plan["bcc"]):
            raise MigrationError("Alias and BCC staging requires inactive records")
    except (KeyError, TypeError) as error:
        raise MigrationError("Invalid account plan fields; contents omitted") from error


def load_saved_plan(file: Path, configuration: Configuration) -> SavedPlan:
    path = local_path(file)
    plan = read_json(path, private=True)
    validate_saved_plan(plan)
    manifest = read_json(path.parent / "manifest.json", private=True)
    assessment = read_json(path.parent / "assessment.json", private=True)
    fingerprint = json_fingerprint(plan, canonical=True)
    if (not isinstance(manifest, dict) or type(manifest.get("version")) is not int or manifest["version"] != 1
            or manifest.get("kind") != "migration-planning"
            or manifest.get("complete") is not True or manifest.get("domain") != plan["domain"]
            or manifest.get("plan_sha256") != fingerprint):
        raise MigrationError("Expected an unchanged plan from a completed planning run")
    if manifest.get("configuration") != configuration.document():
        raise MigrationError("Configuration differs from the saved plan; retain the original batch configuration and state")
    sources = tuple(source for source in configured_sources(configuration) if asdict(source) == manifest.get("source"))
    if len(sources) != 1 or plan["source"].get("kind", "postfixadmin") != sources[0].kind.value:
        raise MigrationError("Saved plan source differs from its manifest or configured connection")
    if (not isinstance(assessment, dict) or type(assessment.get("version")) is not int or assessment["version"] != 1
            or assessment.get("kind") != "account-planning-assessment"
            or assessment.get("plan_sha256") != fingerprint or not isinstance(assessment.get("checks"), list)
            or any(not isinstance(row, dict) or any(not isinstance(row.get(key), str) for key in ("id", "status", "detail"))
                   for row in assessment["checks"])):
        raise MigrationError("Missing or inconsistent saved planning assessment")
    register_batch(path.parent)
    return SavedPlan(path.parent, plan, assessment, fingerprint, sources[0])
