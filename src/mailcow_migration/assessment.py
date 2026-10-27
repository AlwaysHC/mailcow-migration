import os
import shutil
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from uuid import uuid4

from .accounts import account_journal, activation_started, run_accounts
from .configuration import Configuration
from .core.errors import ConfigurationError, MigrationError
from .core.state import Workspace
from .core.storage import json_fingerprint, read_json, write_private_json
from .core.timezones import timezone_parts
from .handoff import run_handoff
from .plans.evidence import load_plan_export
from .plans.saved import load_saved_plan, validate_plan_inventory
from .plans.saved_webmail import load_saved_webmail
from .plans.security import account_security_policy
from .plans.server_state import require_server_settings
from .plans.transfer import transfer_assessment
from .sogo_target import sogo_preparation
from .source_drift import verify_source_plan
from .sources import SourceKind
from .sources.delivery import SourceDelivery
from .sources.inventory import inspect_source
from .sources.logins import SourceLogins
from .sources.plesk_isolation import PleskIsolation
from .sources.roundcube import roundcube_sessions
from .sources.submission import SourceSubmission
from .targets.isolation import TargetIsolation
from .targets.mailcow import MailcowTarget
from .targets.passwords import check_password_support
from .targets.sieve_runtime import SieveRuntime
from .targets.sogo import configuration_blockers, missing_custom_from
from .targets.sogo_settings import verify_container_dates
from .targets.sogo_values import custom_identity_configuration
from .targets.storage import inspect_mail_storage
from .transfers import TransferContext, require_mailcow_host
from .webmail import run_webmail


def assess_batch(
    configuration: Configuration, file: Path, *, exported: Optional[Path] = None, phase: str = "before", hostname: Optional[str] = None,
) -> Mapping[str, Any]:
    require_mailcow_host()
    if phase not in ("before", "staged", "activated", "handoff"):
        raise ConfigurationError("Choose assessment phase before, staged, activated or handoff")
    with Workspace().lock():
        batch = load_saved_plan(file, configuration, allow_blockers=True)
        checks = []

        def check(name: str, operation: Callable[[], Any]) -> Any:
            print("Assessing " + name + "...", flush=True)
            try:
                value = operation()
            except (MigrationError, OSError) as error:
                checks.append({"id": name, "status": "blocked", "detail": str(error)})
                return None
            checks.append({"id": name, "status": "not-needed" if value is False else "passed", "detail": "Verified"})
            return value

        def require_plan() -> None:
            if batch.plan["blockers"] or batch.blocking_findings:
                raise MigrationError("\n".join((*batch.plan["blockers"], batch.blocking_findings)))

        check("saved-plan-findings", require_plan)
        state = check("account-journal", lambda: account_journal(batch, configuration).read())
        if state is not None:
            check("saved-security-policy", lambda: account_security_policy(batch, state))
        check("shared-server-settings", lambda: require_server_settings(batch, configuration))
        manifest = read_json(batch.directory / "manifest.json", private=True)
        evidence = check("source-export", lambda: load_plan_export(configuration, batch, exported or Path(manifest["export"])))
        source = check("live-source", lambda: inspect_source(batch.source, batch.plan["domain"]))
        if source is not None:
            check("live-source-inventory", lambda: validate_plan_inventory(batch.plan, source.inventory))
        target = MailcowTarget(configuration.mailcow)
        if source is not None and phase == "before" and not batch.plan["blockers"]:
            transfer = check("live-transfer-preview", lambda: transfer_assessment(
                batch.plan, source, target, batch.directory / ("assessment-transfer-" + uuid4().hex), source_checks=True,
            ))
            if transfer is not None:
                checks.extend({**row, "id": "transfer:" + row["id"]} for row in transfer["checks"])
        if phase in ("before", "staged"):
            check("target-password-schemes", lambda: check_password_support(batch.plan, target))
        storage = check("target-mail-volume", lambda: inspect_mail_storage(target))
        if storage is not None and phase in ("before", "staged"):
            free = shutil.disk_usage(storage.root).free
            filesystem = os.statvfs(storage.root)
            usage = batch.plan.get("sourceUsage", {}).get("mailboxes")
            if isinstance(usage, list):
                required = sum(max(row["upperBoundBytes"], row.get("storedBytes") or 0) + row["messages"] * filesystem.f_frsize for row in usage)
                detail = "{} available bytes; {} estimated message bytes including block allowance. Review growth and metadata."
                checks.append({"id": "storage-capacity", "status": "blocked" if required > free else "passed",
                               "detail": detail.format(free, required)})
            else:
                detail = "{} available bytes; fixed-quota planning has no measured source size. Operator capacity review is required."
                checks.append({"id": "storage-capacity", "status": "manual", "detail": detail.format(free)})
            detail = "{} available inodes; review source folder/message counts and concurrent target growth."
            checks.append({"id": "storage-inodes", "status": "blocked" if filesystem.f_favail == 0 else "manual",
                           "detail": detail.format(filesystem.f_favail)})
        if phase == "before":
            if state is not None and activation_started(state):
                checks.append({"id": "phase", "status": "blocked", "detail": "Activation started; use activated or handoff assessment"})
            else:
                check("source-account-filter-snapshot", lambda: verify_source_plan(batch, configuration))
                check("target-staging-preview", lambda: run_accounts(configuration, file))
                check("target-isolation-environment", lambda: TargetIsolation(target, batch).preflight(require_accounts=False))
                if batch.source.kind is SourceKind.PLESK:
                    isolation_source = PleskIsolation(batch, configuration)
                    check("source-isolation-environment", lambda: isolation_source.request("inspect"))
                else:
                    if batch.plan["mailboxes"]:
                        check("source-login-environment", SourceLogins(batch).inspect)
                    check("source-submission-environment", SourceSubmission(batch, configuration).inspect)
                    check("source-delivery-environment", SourceDelivery(batch, configuration.mailcow.api).inspect)
        elif phase in ("staged", "activated"):
            context = TransferContext(batch, configuration)
            check("account-inventory", lambda: context.check_accounts(target, activated=phase == "activated"))
            check("source-target-isolation", lambda: context.check_holds(target, webmail=phase == "activated"))
            if phase == "activated":
                check("final-mail-sieve", context.final_state)
        if phase != "handoff":
            check("reviewed-roundcube-source", lambda: roundcube_sessions(batch, configuration).preflight(
                require_drafts=phase in ("staged", "activated"),
            ))
        reviewed = (check("saved-webmail-proposal", lambda: load_saved_webmail(batch, evidence))
                    if evidence is not None and phase != "handoff" else None)
        prepared = check("sogo-preparation", lambda: sogo_preparation(configuration, batch)) if phase != "handoff" else None
        if prepared is not None and reviewed is not None:
            def capabilities() -> Mapping[str, Any]:
                if reviewed.blockers:
                    raise MigrationError("\n".join(reviewed.blockers))
                native = {row["username"]: row for row in reviewed.proposal["nativeSettings"]["users"]}
                users = [*prepared.users, *[
                    {**row, "preferences": {**row.get("preferences", {}), **native.get(row["username"], {}).get("preferences", {})}}
                    for row in reviewed.proposal["users"] if batch.source.kind is SourceKind.POSTFIXADMIN
                ]]
                if not users:
                    return {"configuration_required": False}
                config = prepared.target.probe()
                missing = missing_custom_from(users, config)
                blockers = configuration_blockers(users, custom_identity_configuration(config, missing))
                if blockers:
                    raise MigrationError("\n".join(blockers))
                if batch.source.kind is SourceKind.POSTFIXADMIN and reviewed.proposal["users"]:
                    runtime = SieveRuntime(target, batch.directory / "sogo-preflight")
                    runtime.preflight()
                    zone = reviewed.policy["vacationTimeZone"]
                    dates = timezone_parts(zone, [user["preferences"]["Vacation"][key] for user in native.values()
                                                  if "Vacation" in user["preferences"] for key in ("startDate", "endDate")])
                    verify_container_dates((prepared.target.command, runtime.command), dates, zone)
                configuration_state = prepared.state()
                return {"configuration_required": bool(missing or configuration_state and configuration_state["pending"])}

            capabilities_report = check("sogo-capabilities-timezones", capabilities)
            if capabilities_report and capabilities_report["configuration_required"]:
                checks.append({"id": "sogo-configuration", "status": "required",
                               "detail": "Review and apply the SOGo custom-identity configuration before isolation"})
            if phase == "activated":
                check("native-user-data-conflicts", lambda: run_webmail(
                    configuration, file, evidence.directory, writers_stopped=True,
                ))
            else:
                checks.append({"id": "native-user-data-conflicts", "status": "deferred",
                               "detail": "Native user-data checks follow activation under target isolation, before webmail writes"})
        if phase == "handoff":
            check("live-handoff", lambda: run_handoff(configuration, file, hostname=hostname, action="check"))
        report = {"version": 1, "kind": "migration-assessment", "phase": phase, "plan": batch.fingerprint,
                  "source": asdict(batch.source), "target": asdict(configuration.mailcow), "checks": checks,
                  "saved_checks": batch.assessment["checks"], "policy_sha256": json_fingerprint(
                      read_json(batch.directory / "policy.json", private=True), canonical=True,
                  ), "ready_for_phase": not any(row["status"] in ("blocked", "required") for row in checks),
                  "migration_approval": False, "checked_at": datetime.now(timezone.utc).isoformat()}
        write_private_json(batch.directory / ("assessment-" + phase + ".json"), report, replace=True)
        for row in checks:
            print("[{}] {}: {}".format(row["status"], row["id"], row["detail"]))
        print("Assessment: {}. This reports the selected phase; later checks and explicit operator approvals remain required.".format(
            batch.directory / ("assessment-" + phase + ".json"),
        ))
        return report


def run_assessment(
    configuration: Configuration, file: Path, *, exported: Optional[Path] = None, phase: str = "before", hostname: Optional[str] = None,
) -> int:
    return int(not assess_batch(configuration, file, exported=exported, phase=phase, hostname=hostname)["ready_for_phase"])
