import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional, Tuple

from .accounts import account_journal
from .configuration import Configuration, section_label
from .core.dns import authoritative_addresses, hostname, normalized_ip
from .core.domain import domain_name
from .core.errors import ConfigurationError, MigrationError
from .core.state import Workspace, require_identity
from .core.storage import local_path, read_json, write_private_json
from .inspection import InspectionReport, inspect_domain, require_inspection_host
from .plans.catalog import known_batches, require_batch_ownership
from .plans.saved import SavedPlan, load_saved_plan, validate_plan_inventory
from .targets.mailcow import MailcowTarget


def target_identity(batch: SavedPlan, configuration: Configuration) -> Mapping[str, object]:
    return {"version": 1, "kind": "source-selection-target", "plan": batch.fingerprint, "source": asdict(batch.source),
            "api": configuration.mailcow.api, "mailcow_directory": configuration.mailcow.directory}


def saved_target_addresses(batch: SavedPlan, configuration: Configuration) -> Tuple[str, ...]:
    file = local_path(batch.directory / "source-selection-target.json")
    if not file.exists():
        return ()
    evidence = read_json(file, private=True)
    if (not isinstance(evidence, dict) or type(evidence.get("version")) is not int
            or not isinstance(evidence.get("hostname"), str) or not isinstance(evidence.get("ips"), list)
            or not evidence["ips"] or any(not isinstance(address, str) for address in evidence["ips"])):
        raise MigrationError("Invalid saved target-address evidence: " + str(file))
    require_identity(evidence, target_identity(batch, configuration))
    hostname(evidence["hostname"])
    addresses = tuple(normalized_ip(address) for address in evidence["ips"])
    if len(set(addresses)) != len(addresses):
        raise MigrationError("Duplicate addresses in saved target evidence")
    state = account_journal(batch, configuration).read()
    if state.get("activated") is not True and "enable domain for activation" not in state["done"]:
        return ()
    return addresses


def record_target_addresses(batch: SavedPlan, configuration: Configuration) -> None:
    state = account_journal(batch, configuration).read()
    if state.get("activated") is not True or state.get("pending"):
        raise MigrationError("Complete account activation before recording target addresses for DNS resume")
    file = local_path(batch.directory / "source-selection-target.json")
    target = MailcowTarget(configuration.mailcow)
    name = hostname(target.compose(("exec", "-T", "postfix-mailcow", "postconf", "-h", "myhostname")).stdout.strip())
    addresses = authoritative_addresses(name)
    if file.exists():
        previous = saved_target_addresses(batch, configuration)
        if read_json(file, private=True)["hostname"] != name or set(previous) != set(addresses):
            raise MigrationError("Target hostname or addresses changed; retain the recorded evidence and reconcile before DNS handoff")
        print("Retaining existing target-address evidence: " + str(file))
        return
    write_private_json(file, {**target_identity(batch, configuration), "hostname": name, "ips": addresses,
                              "checked_at": datetime.now(timezone.utc).isoformat()})
    print("Target addresses recorded for source selection after DNS handoff: " + ", ".join(addresses))
    print("This address snapshot does not verify certificates, SMTP delivery or completion of handoff.")


def select_domain_source(
    configuration: Configuration, domain: str, workspace: Workspace, plan: Optional[Path] = None,
) -> Tuple[InspectionReport, Optional[SavedPlan]]:
    supplied = local_path(plan) if plan is not None else None
    if supplied is not None and supplied.name != "plan.json":
        raise ConfigurationError("Select the original plan.json inside its saved batch directory")
    batches = known_batches(workspace, domain, supplied.parent if supplied is not None else None)
    for candidate in batches:
        print("Known batch: {} (planning {}; {} operation journals).".format(
            candidate.directory, "complete" if candidate.manifest["complete"] else "incomplete", len(candidate.operations),
        ))
    active = tuple(batch for batch in batches if batch.operations)
    if len(active) > 1:
        raise MigrationError("Multiple batches have operation journals for this domain; reconcile them before source selection:\n"
                             + "\n".join(str(batch.directory) for batch in active))
    chosen = supplied.parent if supplied is not None else active[0].directory if active else None
    if chosen is not None:
        require_batch_ownership(batches, chosen)
    saved = load_saved_plan(chosen / "plan.json", configuration) if chosen is not None else None
    if saved is not None:
        state = account_journal(saved, configuration).read()
        if any(key in state and type(state[key]) is not bool for key in ("accounts", "activated")):
            raise MigrationError("Invalid saved account completion markers; retain the batch")
    targets = {saved.source.kind.value: saved_target_addresses(saved, configuration)} if saved is not None else {}
    report = inspect_domain(configuration, domain, target_addresses=targets)
    if report.findings:
        raise MigrationError("Source selection is blocked:\n" + "\n".join(report.findings))
    selected = next(source for source in report.sources if source.connection.kind.value == report.matching_sources[0])
    conflicts = tuple(batch for batch in active if batch.manifest["source"] != asdict(selected.connection))
    if conflicts:
        raise MigrationError("DNS selects a different source from an existing operation journal; retain and reconcile:\n"
                             + "\n".join(str(file) for batch in conflicts for file in batch.operations))
    if saved is not None:
        if saved.source != selected.connection:
            raise MigrationError("DNS selects a different source from the saved plan; no batch was retargeted")
        validate_plan_inventory(saved.plan, selected.inventory)
    workspace.write("source-detection/{}.json".format(domain), {
        **report.document(), "kind": "migration-source-selection", "configuration": configuration.document(),
        "selected_source": asdict(selected.connection), "plan": str(saved.directory / "plan.json") if saved is not None else None,
        "plan_sha256": saved.fingerprint if saved is not None else None,
        "resume_target_ips": targets.get(selected.connection.kind.value, ()),
        "operation_files": [str(file) for batch in batches for file in batch.operations],
        "batches": [{"directory": str(batch.directory), "planning_complete": batch.manifest["complete"],
                     "source_kind": batch.manifest["source"]["kind"]} for batch in batches],
    })
    return report, saved


def run_source_selection(
    configuration: Configuration, domain: str, *, plan: Optional[Path] = None, record_target: bool = False,
) -> int:
    domain_name(domain)
    require_inspection_host()
    if os.geteuid() != 0:
        raise ConfigurationError("Run source selection as root on the Linux Mailcow host")
    if record_target and plan is None:
        raise ConfigurationError("Use --plan when recording target addresses for DNS resume")
    workspace = Workspace()
    with workspace.lock():
        report, batch = select_domain_source(configuration, domain, workspace, plan)
        if record_target:
            if batch is None:
                raise MigrationError("Target address recording requires the original saved batch")
            record_target_addresses(batch, configuration)
        print("Selected {}. Evidence: {}.".format(section_label(report.matching_sources[0]),
                                                  workspace.path("source-detection/{}.json".format(domain))))
        if batch is not None:
            print("Continue with the original plan and configuration: " + str(batch.directory / "plan.json"))
    print("Source selection does not copy mail, resume pending writes or establish migration readiness.")
    return 0
