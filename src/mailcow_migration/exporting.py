import hashlib
import stat
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping
from uuid import uuid4

from .configuration import Configuration, section_label
from .accounts import account_journal, require_before_activation
from .core.domain import domain_name
from .core.errors import MigrationError
from .core.state import Workspace
from .core.storage import private_directory, write_private_json
from .inspection import require_inspection_host
from .source_selection import select_domain_source
from .sources import SourceKind
from .sources.export import plesk_export, postfixadmin_export, postfixadmin_locations, validate_postfixadmin_export
from .sources.inventory import inspect_source


def private_export_files(directory: Path, *, secure: bool = True) -> Iterator[Path]:
    if not stat.S_ISDIR(directory.lstat().st_mode):
        raise MigrationError("Export evidence must be a directory without symlinks")
    if secure:
        directory.chmod(0o700)
    elif directory.stat().st_mode & 0o077:
        raise MigrationError("Export directories must have owner-only permissions")
    for path in sorted(directory.iterdir()):
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            yield from private_export_files(path, secure=secure)
        elif stat.S_ISREG(mode):
            if secure:
                path.chmod(0o600)
            elif mode & 0o077:
                raise MigrationError("Export files must have owner-only permissions")
            yield path
        else:
            raise MigrationError("Export contains a symlink or unsupported filesystem entry")


def export_file_manifest(directory: Path, *, secure: bool = True) -> Mapping[str, Any]:
    files = {}
    for path in private_export_files(directory, secure=secure):
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        files[path.relative_to(directory).as_posix()] = {"size": path.stat().st_size, "sha256": digest.hexdigest()}
    if not files:
        raise MigrationError("Export contains no source evidence")
    return files


def run_export(configuration: Configuration, domain: str) -> int:
    domain_name(domain)
    require_inspection_host()
    workspace = Workspace()
    with workspace.lock():
        report, batch = select_domain_source(configuration, domain, workspace)
        if batch is not None:
            require_before_activation(account_journal(batch, configuration).read())
        source = next(source for source in report.sources if source.connection.kind.value == report.matching_sources[0])
        postfixadmin = source.connection.kind is SourceKind.POSTFIXADMIN
        if postfixadmin:
            require_inspection_host(("scp",))
        identifier = uuid4().hex
        destination = workspace.path("exports/{}/{}".format(domain, identifier))
        private_directory(destination.parent)
        destination.mkdir(mode=0o700)
        manifest = {
            "version": 1, "kind": "source-export", "export_id": identifier, "domain": domain, "complete": False,
            "started_at": datetime.now(timezone.utc).isoformat(), "source": asdict(source.connection),
            "mail_root": source.inventory.mail_root, "ips": source.ips,
            "remote": postfixadmin_locations(identifier) if postfixadmin else None,
        }
        try:
            write_private_json(destination / "manifest.json", manifest)
            write_private_json(destination / "inspection.json", report.document())
            print("Exporting {} configuration from {}...".format(section_label(source.connection.kind.value), source.connection.ssh.host))
            print("Export progress and diagnostics are kept privately in {}.".format(destination))
            blockers = (postfixadmin_export(source, domain, destination, identifier) if postfixadmin
                        else plesk_export(source, domain, destination))
            files = export_file_manifest(destination / "data")
            if postfixadmin:
                validate_postfixadmin_export(destination / "data", domain, source.inventory.mail_root)
            print("Rechecking source domain and Maildir inventory...")
            if inspect_source(source.connection, domain) != source:
                raise MigrationError("Source inventory changed during export; keep the evidence and review the source before exporting again")
            write_private_json(destination / "manifest.json", {
                **manifest, "complete": True, "completed_at": datetime.now(timezone.utc).isoformat(),
                "files": files, "source_reported_blockers": blockers,
            }, replace=True)
        except (MigrationError, OSError, KeyboardInterrupt):
            print("Export incomplete. Files and any remote staging artifacts were retained; see {}.".format(destination))
            raise
    print("Private source export saved to {}.".format(destination))
    if blockers:
        print("Preserved {} source-reported blockers; review them in the private snapshot before planning.".format(len(blockers)))
    print("Export completion is not migration readiness. Accounts, messages and mail services were not changed.")
    return 0
