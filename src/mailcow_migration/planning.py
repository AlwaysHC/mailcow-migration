import math
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from .configuration import Configuration
from .core.domain import domain_name
from .core.errors import ConfigurationError, MigrationError
from .core.state import Workspace
from .core.storage import json_fingerprint, local_path, private_directory, read_json, write_private_json, write_private_text
from .inspection import require_inspection_host
from .plans.assessment import check
from .plans.domain import MIB, QUOTA_LIMIT, active, build_plan, number
from .plans.evidence import ExportEvidence, load_export
from .plans.plesk import plesk_plan
from .plans.policy import load_policy, policy_plan
from .plans.saved import validate_plan_inventory
from .plans.transfer import transfer_assessment
from .sources import SourceKind
from .sources.inventory import inspect_source
from .sources.sql import parse_sql
from .sources.usage import apply_source_usage, measure_source
from .targets.mailcow import MailcowTarget
from .targets.readiness import target_assessment


def source_plan(configuration: Configuration, evidence: ExportEvidence, fixed_quota: Optional[int]) -> Mapping[str, Any]:
    domain = evidence.manifest["domain"]
    try:
        if evidence.source.connection.kind is SourceKind.PLESK:
            if fixed_quota is not None:
                raise ConfigurationError("Plesk planning uses measured quotas; the fixed-quota override applies only to PostfixAdmin")
            return plesk_plan(read_json(evidence.data / "source.json", private=True), configuration.migration)
        parsed = parse_sql(evidence.data / "source-settings.sql")
        rows = parsed["rows"]
        if not isinstance(rows.get("mailbox"), list):
            raise MigrationError("Missing source table mailbox")
        quota = fixed_quota if fixed_quota is not None else max([
            1, *(math.ceil(number(row["quota"]) / MIB) for row in rows["mailbox"] if row["domain"] == domain),
        ])
        plan = build_plan(rows, domain, quota, configuration.migration, evidence.data / "config")
        return {**plan, "source": {**plan["source"], "vacationDatesUtc": parsed["vacationDatesUtc"]}}
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise MigrationError("Source export has invalid or missing planning fields; contents omitted") from error


def planning_summary(
    plan: Mapping[str, Any], assessment: Mapping[str, Any], account_policy: Mapping[str, Any], transfer: Mapping[str, Any],
) -> str:
    target = assessment["target"]
    lines = [
        "Domain: " + plan["domain"],
        "Target domain action: " + target["domainAction"],
        "Quota calculation: " + assessment["quotaMode"],
        "Domain quota: {} MiB; default {} MiB; maximum mailbox {} MiB".format(
            plan["domainPayload"]["quota"], plan["domainPayload"]["defquota"], plan["domainPayload"]["maxquota"]),
        "", "Proposed account changes (no changes applied):",
    ]
    lines.extend("  {}: {} -> {}".format(row["field"], row["before"], row["after"]) for row in target["domainChanges"])
    lines.extend("  Mailbox {}: {} MiB; source {}; stage inactive; quarantine {}/{}".format(
        row["username"], row["payload"]["quota"], "active" if row["active"] else "inactive",
        row["payload"]["quarantine_notification"], row["payload"]["quarantine_category"],
    ) for row in plan["mailboxes"])
    lines.extend("  Alias {} -> {}; source {}".format(
        row["address"], row["payload"].get("goto", "discard"), "active" if row["active"] else "inactive",
    ) for row in plan["aliases"] if not row["mailbox"])
    lines.extend("  Alias domain {} -> {}; source {}".format(row["alias_domain"], row["target_domain"],
                 "active" if active(row["active"]) else "inactive") for row in plan["aliasDomains"])
    lines.extend("  Sender BCC {} -> {}".format(row["local_dest"], row["bcc_dest"]) for row in plan["bcc"])
    lines.extend("  Forward {} -> {}; keep local copy: {}".format(row["username"], ", ".join(row["destinations"]), row["keepLocal"])
                 for row in account_policy["forwardings"])
    lines.extend("  Recovery email: {}".format(row["username"]) for row in account_policy["recovery"])
    lines.extend("  Administrator {}: {}".format(row["username"], ", ".join(row["domains"])) for row in account_policy["admins"])
    lines.extend("  Send-as {}: {}".format(row["username"], ", ".join(row["senders"])) for row in account_policy["senderPermissions"])
    webmail = account_policy["webmail"]
    lines.extend(("", "Proposed webmail changes (no changes applied):"))
    lines.extend("  {}: {}".format(row["username"], "Sieve mapping blocked" if "mappingError" in row
                 else "{} editable SOGo rules".format(len(row["filters"])) if "filters" in row
                 else "preserve personal Sieve" if row["script"] is not None else "no personal Sieve") for row in webmail["users"])
    lines.extend("  {}: {}".format(row["username"], ", ".join([
        *("SOGo " + name for name in row["preferences"]), *(["Mailcow forwarding prefilter"] if "prefilter" in row else []),
    ])) for row in webmail["nativeSettings"]["users"])
    lines.append("  {} inactive staging prefilters; detailed proposals in webmail-plan.json".format(len(webmail["prefilters"])))
    shared = account_policy["sharedSettings"]
    counts = shared["counts"]
    lines.extend(("", "Shared server settings (no changes applied):",
                  "  {} supported mappings; {} unsupported; {} blockers; {} informational".format(
                      counts["automatic"], counts["unsupported"], counts["blocker"], counts["adapted"]),
                  "  Source values, domain relationships and per-service sender findings: shared-settings-plan.json"))
    storage = transfer["storage"]
    lines.extend(("", "Mail storage and transfer preview (no files copied):",
                  "  Mail volume: {}; vmail identity {}:{}".format(storage["containerRoot"], storage["uid"], storage["gid"]),
                  "  {} mailbox records; detailed findings in transfer-plan.json".format(len(transfer["mailboxes"]))))
    lines.extend(("", "Assessment:"))
    lines.extend("  [{}] {}: {}".format(row["status"], row["id"], row["detail"]) for row in assessment["checks"])
    lines.extend("  " + reason for reason in target["conflicts"]["reasons"])
    lines.extend(("", "Planning notes:", *("  " + note for note in plan["notes"]), "",
                  "Planning is not migration approval. Pending assessments must be implemented and completed before migration.",
                  "This plan does not resume an existing migration batch; retain its saved state and recovery evidence."))
    return "\n".join(lines) + "\n"


def run_plan(configuration: Configuration, domain: str, exported: Path, output: Path, fixed_quota: Optional[int] = None) -> int:
    domain_name(domain)
    require_inspection_host()
    if os.geteuid() != 0:
        raise ConfigurationError("Run planning as root on the Linux Mailcow host")
    if fixed_quota is not None and not 1 <= fixed_quota <= QUOTA_LIMIT:
        raise ConfigurationError("--quota-mib must be between 1 and 1000000000")
    destination = local_path(output)
    export_directory = local_path(exported)
    if destination == export_directory or export_directory in destination.parents:
        raise ConfigurationError("Plan output must be outside the source export directory")
    if destination.exists():
        raise ConfigurationError("Plan output exists; choose a new --out directory")
    workspace = Workspace()
    with workspace.lock():
        print("Validating completed export and source identity...")
        evidence = load_export(configuration, domain, export_directory)
        policy = load_policy(configuration.migration.policy_file)
        plan = source_plan(configuration, evidence, fixed_quota)
        validate_plan_inventory(plan, evidence.source.inventory)
        account_policy = policy_plan(plan, evidence, policy)
        private_directory(destination.parent)
        destination.mkdir(mode=0o700)
        provenance = {"version": 1, "kind": "migration-planning", "complete": False, "domain": domain,
                      "export": str(evidence.directory), "export_id": evidence.manifest["export_id"],
                      "source": asdict(evidence.source.connection), "configuration": configuration.document(),
                      "started_at": datetime.now(timezone.utc).isoformat()}
        write_private_json(destination / "manifest.json", provenance)
        try:
            checks = [check("source-mapping", "blocked", item) for item in plan["blockers"]]
            quota_mode = "fixed" if fixed_quota is not None else "pending-source-blockers" if plan["blockers"] else "measured"
            if quota_mode == "measured":
                print("Checking current source inventory before sizing...")
                if inspect_source(evidence.source.connection, domain) != evidence.source:
                    raise MigrationError("Source inventory changed since export; export the current source again")
                plan = apply_source_usage(plan, measure_source(plan, evidence.source, configuration.migration, destination))
                if inspect_source(evidence.source.connection, domain) != evidence.source:
                    raise MigrationError("Source inventory changed during sizing; keep the evidence and export again")
            elif quota_mode == "pending-source-blockers":
                checks.append(check("source-size", "blocked", "Resolve source mapping blockers before sizing; displayed quotas are provisional"))
            else:
                checks.append(check("source-size", "required", "Explicit fixed quota: source sizes were not measured; verify usage before migration"))
            if plan["domainPayload"]["quota"] > QUOTA_LIMIT:
                checks.append(check("domain-quota", "blocked", "Calculated domain quota is too large"))
            print("Checking Mailcow services and existing account inventories...")
            mailcow = MailcowTarget(configuration.mailcow)
            target = target_assessment(plan, mailcow, account_policy, policy)
            print("Inspecting mail storage and read-only transfer prerequisites...")
            transfer = transfer_assessment(plan, evidence.source, mailcow, destination, source_checks=quota_mode == "measured")
            evidence.verify()
            checks.extend([*account_policy["checks"], *target["checks"], *transfer["checks"]])
            assessment = {"version": 1, "kind": "account-planning-assessment", "checked_at": datetime.now(timezone.utc).isoformat(),
                          "plan_sha256": json_fingerprint(plan, canonical=True), "migration_ready": False,
                          "quotaMode": quota_mode, "checks": checks, "target": target,
                          "webmail_plan": "webmail-plan.json", "shared_settings_plan": "shared-settings-plan.json",
                          "transfer_plan": "transfer-plan.json"}
            write_private_json(destination / "plan.json", plan)
            write_private_json(destination / "policy.json", policy)
            reports = {"webmail": "webmail-plan.json", "sharedSettings": "shared-settings-plan.json"}
            write_private_json(destination / "account-policy.json", {key: value for key, value in account_policy.items() if key not in reports})
            for key, file in reports.items():
                write_private_json(destination / file, account_policy[key])
            write_private_json(destination / "transfer-plan.json", transfer)
            write_private_json(destination / "assessment.json", assessment)
            write_private_json(destination / "domains.json", plan["domains"])
            write_private_text(destination / "users.txt", "\n".join(row["username"] for row in plan["mailboxes"]) + "\n")
            summary = planning_summary(plan, assessment, account_policy, transfer)
            write_private_text(destination / "summary.txt", summary)
            write_private_json(destination / "manifest.json", {**provenance, "complete": True,
                               "completed_at": datetime.now(timezone.utc).isoformat(), "plan_sha256": assessment["plan_sha256"]}, replace=True)
        except (MigrationError, OSError, KeyboardInterrupt):
            print("Planning incomplete. Retained evidence and diagnostics in {}.".format(destination))
            raise
    print(summary)
    print("Private plan and assessment saved to {}.".format(destination))
    return int(any(row["status"] in ("blocked", "decision") for row in checks))
