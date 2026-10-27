from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from ..configuration import Configuration, mail_root
from ..core.errors import MigrationError
from ..core.storage import local_path, read_json, read_text
from ..exporting import export_file_manifest
from ..sources import SourceKind, configured_sources
from ..sources.export import validate_plesk_snapshot, validate_postfixadmin_export
from ..sources.inventory import DomainInventory, SourceInspection
from .saved import SavedPlan, validate_plan_inventory


@dataclass(frozen=True)
class ExportFiles:
    directory: Path
    manifest: Mapping[str, Any]

    @property
    def data(self) -> Path:
        return self.directory / "data"

    def verify(self) -> None:
        if read_json(self.directory / "manifest.json", private=True) != self.manifest:
            raise MigrationError("Export manifest changed during this operation")
        if export_file_manifest(self.data, secure=False) != self.manifest["files"]:
            raise MigrationError(
                "Export files differ from their completion manifest; retain the export and investigate"
            )


@dataclass(frozen=True)
class ExportEvidence(ExportFiles):
    source: SourceInspection


def optional_export_text(evidence: ExportFiles, *parts: str) -> Optional[str]:
    file = local_path(evidence.data.joinpath(*parts))
    if evidence.data not in file.parents:
        raise MigrationError("Evidence must stay inside the source export")
    try:
        return read_text(file, private=True) if file.exists() else None
    except UnicodeError as error:
        raise MigrationError(
            "Exported text must contain valid UTF-8; original files retained"
        ) from error


def load_export(
    configuration: Configuration, domain: str, directory: Path
) -> ExportEvidence:
    directory = local_path(directory)
    manifest = read_json(directory / "manifest.json", private=True)
    if (
        not isinstance(manifest, dict)
        or type(manifest.get("version")) is not int
        or manifest["version"] != 1
        or manifest.get("kind") != "source-export"
        or manifest.get("domain") != domain
        or manifest.get("complete") is not True
        or not isinstance(manifest.get("export_id"), str)
        or not manifest["export_id"]
        or not isinstance(manifest.get("files"), dict)
        or not manifest["files"]
        or not isinstance(manifest.get("source_reported_blockers"), list)
        or any(
            not isinstance(item, str) for item in manifest["source_reported_blockers"]
        )
    ):
        raise MigrationError("Expected a completed source export for this domain")
    connection = next(
        (
            source
            for source in configured_sources(configuration)
            if asdict(source) == manifest.get("source")
        ),
        None,
    )
    if connection is None:
        raise MigrationError(
            "Export source connection differs from the selected configuration"
        )
    root = mail_root(manifest.get("mail_root"))
    if connection.kind is SourceKind.POSTFIXADMIN and root != connection.mail_root:
        raise MigrationError(
            "Export mail root differs from the configured PostfixAdmin source"
        )
    report = read_json(directory / "inspection.json", private=True)
    if (
        not isinstance(report, dict)
        or report.get("version") != 1
        or report.get("kind") != "source-inspection"
        or report.get("domain") != domain
        or report.get("findings") != []
        or report.get("matching_sources") != [connection.kind.value]
        or not isinstance(report.get("sources"), list)
    ):
        raise MigrationError("Export is missing its successful source inspection")
    matches = [
        row
        for row in report["sources"]
        if isinstance(row, dict)
        and row.get("kind") == connection.kind.value
        and row.get("host") == connection.ssh.host
    ]
    if len(matches) != 1 or not isinstance(matches[0].get("inventory"), dict):
        raise MigrationError(
            "Export source inspection does not identify one configured source"
        )
    saved = matches[0]
    inventory = DomainInventory.from_document(
        {**saved["inventory"], "mailRoot": saved["inventory"].get("mail_root")}
    )
    if (
        inventory.mail_root != root
        or inventory.primary != domain
        or not inventory.present
        or not inventory.active
        or saved.get("missing_maildirs") != []
        or not isinstance(saved.get("ips"), list)
        or saved["ips"] != manifest.get("ips")
        or type(saved.get("domain_directory")) is not bool
    ):
        raise MigrationError("Export source inventory or identity is inconsistent")
    evidence = ExportEvidence(
        directory,
        manifest,
        SourceInspection(
            connection,
            inventory,
            tuple(saved["ips"]),
            saved["domain_directory"],
            (),
        ),
    )
    evidence.verify()
    if connection.kind is SourceKind.POSTFIXADMIN:
        validate_postfixadmin_export(evidence.data, domain, root)
    else:
        snapshot = read_json(evidence.data / "source.json", private=True)
        if (
            list(validate_plesk_snapshot(snapshot, evidence.source, domain))
            != manifest["source_reported_blockers"]
        ):
            raise MigrationError(
                "Plesk source blockers differ from the completion manifest"
            )
    return evidence


def load_plan_export(
    configuration: Configuration, batch: SavedPlan, directory: Path
) -> ExportEvidence:
    evidence = load_export(configuration, batch.plan["domain"], directory)
    manifest = read_json(batch.directory / "manifest.json", private=True)
    if (
        not isinstance(manifest, dict)
        or evidence.manifest["export_id"] != manifest.get("export_id")
        or evidence.source.connection != batch.source
    ):
        raise MigrationError(
            "Use the completed export selected by this saved plan; retain its original evidence"
        )
    validate_plan_inventory(batch.plan, evidence.source.inventory)
    return evidence
