from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .configuration import Configuration
from .core.errors import ConfigurationError, MigrationError
from .core.state import Workspace, require_identity
from .core.storage import local_path, private_directory, read_json, write_private_json
from .exporting import export_file_manifest
from .inspection import require_inspection_host
from .plans.evidence import ExportFiles
from .sources import SourceConnection, SourceKind, configured_sources
from .sources.export import postfixadmin_locations, postfixadmin_snapshot, validate_postfixadmin_export
from .transfers import require_mailcow_host


def server_source(configuration: Configuration) -> SourceConnection:
    source = next((source for source in configured_sources(configuration) if source.kind is SourceKind.POSTFIXADMIN), None)
    if source is None:
        raise ConfigurationError("Shared server settings require a configured PostfixAdmin source; Plesk panel policy is outside this workflow")
    return source


@dataclass(frozen=True)
class ServerExport(ExportFiles):
    source: SourceConnection


def load_server_export(configuration: Configuration, directory: Path) -> ServerExport:
    source = server_source(configuration)
    directory = local_path(directory)
    manifest = read_json(directory / "manifest.json", private=True)
    if (not isinstance(manifest, dict) or type(manifest.get("version")) is not int or manifest.get("complete") is not True
            or not isinstance(manifest.get("export_id"), str) or not manifest["export_id"] or not isinstance(manifest.get("files"), dict)):
        raise MigrationError("Use a completed shared-server export; retain incomplete evidence")
    require_identity(manifest, {"version": 1, "kind": "server-source-export", "scope": "server", "source": asdict(source)})
    evidence = ServerExport(directory, manifest, source)
    evidence.verify()
    validate_postfixadmin_export(evidence.data, "--server", source.mail_root)
    return evidence


def run_server_export(configuration: Configuration) -> int:
    require_mailcow_host()
    require_inspection_host(("scp",))
    source = server_source(configuration)
    workspace = Workspace()
    with workspace.lock():
        identifier = uuid4().hex
        destination = workspace.path("exports/server/" + identifier)
        private_directory(destination)
        manifest = {"version": 1, "kind": "server-source-export", "scope": "server", "export_id": identifier,
                    "source": asdict(source), "remote": postfixadmin_locations(identifier), "complete": False,
                    "started_at": datetime.now(timezone.utc).isoformat()}
        write_private_json(destination / "manifest.json", manifest)
        try:
            postfixadmin_snapshot(source, source.mail_root, "--server", destination, identifier)
            files = export_file_manifest(destination / "data")
            validate_postfixadmin_export(destination / "data", "--server", source.mail_root)
            write_private_json(destination / "manifest.json", {**manifest, "files": files, "complete": True,
                               "completed_at": datetime.now(timezone.utc).isoformat()}, replace=True)
        except (MigrationError, OSError, KeyboardInterrupt):
            print("Shared export incomplete; retain local and remote evidence: " + str(destination))
            raise
    print("Private shared-server export: {}. Source mail services were not changed.".format(destination))
    return 0
