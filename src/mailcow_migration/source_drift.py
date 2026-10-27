import json
from pathlib import Path
from typing import Any

from .configuration import Configuration
from .core.errors import MigrationError
from .core.storage import read_json, write_private_json
from .exporting import export_domain
from .planning import source_plan
from .plans.evidence import load_export, load_plan_export
from .plans.saved import SavedPlan
from .plans.sieve_evidence import load_sieve_evidence
from .sources import SourceKind
from .sources.plesk_isolation import PleskIsolation


def canonical_inventory(value: Any) -> Any:
    if isinstance(value, list):
        return sorted(
            (canonical_inventory(item) for item in value),
            key=lambda item: json.dumps(item, sort_keys=True),
        )
    if isinstance(value, dict):
        return {key: canonical_inventory(item) for key, item in sorted(value.items())}
    return value


def verify_source_plan(batch: SavedPlan, configuration: Configuration) -> None:
    if batch.source.kind is SourceKind.PLESK:
        PleskIsolation(batch, configuration).check_source()
        return
    print(
        "Refreshing private source account/filter evidence for drift comparison...",
        flush=True,
    )
    manifest = read_json(batch.directory / "manifest.json", private=True)
    original = load_plan_export(configuration, batch, Path(manifest["export"]))
    directory = export_domain(configuration, batch.plan["domain"])
    current = load_export(configuration, batch.plan["domain"], directory)
    planned = source_plan(configuration, current, batch.plan["quota"])
    sections = ("source", "aliases", "aliasDomains", "bcc", "domainActive")
    changed = [
        key
        for key in sections
        if canonical_inventory(batch.plan[key]) != canonical_inventory(planned[key])
    ]
    if load_sieve_evidence(batch.plan, original) != load_sieve_evidence(
        planned, current
    ):
        changed.append("sieve")
    report = {
        "version": 1,
        "kind": "source-plan-drift",
        "plan": batch.fingerprint,
        "export": str(directory),
        "changed_sections": changed,
        "source_blockers": planned["blockers"],
        "complete": not changed and not planned["blockers"],
    }
    write_private_json(batch.directory / "source-current.json", report, replace=True)
    if not report["complete"]:
        raise MigrationError(
            "Source account/filter configuration changed or became unsupported: {}. "
            "Compare the original export with {} privately; no source values were printed.".format(
                ", ".join(changed) or "source blockers",
                directory,
            )
        )
    print(
        "Source accounts, aliases, BCC, administrators, recovery settings and Sieve match the saved plan."
    )
