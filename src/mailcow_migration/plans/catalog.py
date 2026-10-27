import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

from ..configuration import Configuration, configuration_from_document
from ..core.domain import domain_name
from ..core.errors import MigrationError
from ..core.state import Workspace, require_identity
from ..core.storage import json_fingerprint, local_path, read_json
from ..sources import configured_sources

OPERATION_FILES = (
    "state.json",
    "source-logins.json",
    "source-delivery.json",
    "source-submission.json",
    "source-isolation.json",
    "source-forwarding.json",
    "target-isolation.json",
    "webmail-target-isolation.json",
    "transfers.json",
    "prefilters.json",
    "senders.json",
    "admins.json",
    "admin-credentials.json",
    "quota.json",
    "roundcube/state.json",
    "roundcube-sessions/state.json",
    "sogo-target/config-state.json",
    "sogo-target/native-import-state.json",
    "sogo-filters/state.json",
    "sogo-settings/state.json",
    "dkim.json",
    "outgoing-relay.json",
    "signing-choice.json",
    "mail-certificate-request.json",
    "workflow.json",
    "completed.json",
)


@dataclass(frozen=True)
class BatchLocation:
    directory: Path
    manifest: Mapping[str, Any] = field(repr=False)
    configuration: Configuration = field(repr=False)
    operations: Tuple[Path, ...]

    @property
    def binding(self) -> Mapping[str, str]:
        return {
            "domain": self.manifest["domain"],
            "configuration": json_fingerprint(
                self.configuration.document(), canonical=True
            ),
        }


def operation_files(directory: Path) -> Tuple[Path, ...]:
    preparation = local_path(directory / "sogo-target")
    attempts = (
        tuple(
            path
            for path in preparation.iterdir()
            if path.name.startswith("user-preparation-") and path.suffix == ".json"
        )
        if preparation.exists()
        else ()
    )
    files = tuple(local_path(directory / name) for name in OPERATION_FILES) + tuple(
        local_path(path) for path in attempts
    )
    present = tuple(path for path in files if path.exists())
    if any(not path.is_file() for path in present):
        raise MigrationError(
            "An operation journal is not a regular file; retain the batch: "
            + str(directory)
        )
    result = []
    for path in present:
        if path == local_path(directory / "roundcube-sessions/state.json"):
            state = read_json(path, private=True)
            if (
                isinstance(state, dict)
                and type(state.get("version")) is int
                and state["version"] == 1
                and state.get("kind") == "roundcube-session-isolation"
                and state.get("stage") == "preview"
                and state.get("complete") is False
                and state.get("attempts") == []
            ):
                continue
        result.append(path)
    return tuple(result)


def batch_location(directory: Path) -> BatchLocation:
    directory = local_path(directory)
    manifest = read_json(directory / "manifest.json", private=True)
    if (
        not isinstance(manifest, dict)
        or type(manifest.get("version")) is not int
        or manifest["version"] != 1
        or manifest.get("kind") != "migration-planning"
        or type(manifest.get("complete")) is not bool
    ):
        raise MigrationError(
            "Invalid planning manifest; retain the batch: " + str(directory)
        )
    domain_name(manifest.get("domain"))
    configuration = configuration_from_document(manifest.get("configuration"))
    if manifest.get("source") not in tuple(
        asdict(source) for source in configured_sources(configuration)
    ):
        raise MigrationError(
            "Saved source differs from the batch configuration: " + str(directory)
        )
    if manifest["complete"]:
        plan = read_json(directory / "plan.json", private=True)
        if (
            not isinstance(plan, dict)
            or plan.get("domain") != manifest["domain"]
            or manifest.get("plan_sha256") != json_fingerprint(plan, canonical=True)
        ):
            raise MigrationError(
                "Saved plan changed or is missing; retain the batch: " + str(directory)
            )
    return BatchLocation(directory, manifest, configuration, operation_files(directory))


def catalog_entries(workspace: Workspace) -> Mapping[str, Any]:
    if not workspace.path("batch-catalog.json").exists():
        return {}
    catalog = workspace.read("batch-catalog.json")
    require_identity(catalog, {"version": 1, "kind": "migration-batch-catalog"})
    entries = catalog.get("batches")
    if (
        type(catalog.get("version")) is not int
        or not isinstance(entries, dict)
        or any(
            not Path(path).is_absolute()
            or str(local_path(Path(path))) != path
            or not isinstance(binding, dict)
            or set(binding) != {"domain", "configuration"}
            or not isinstance(binding["configuration"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", binding["configuration"])
            for path, binding in entries.items()
        )
    ):
        raise MigrationError(
            "Invalid batch catalog; retain it and reconcile the saved paths"
        )
    for binding in entries.values():
        domain_name(binding["domain"])
    return entries


def planning_directories(directory: Path) -> Tuple[Path, ...]:
    directory = local_path(directory)
    if not directory.exists():
        return ()
    if (
        local_path(directory / "manifest.json").exists()
        or local_path(directory / "plan.json").exists()
    ):
        return (directory,)
    return tuple(
        found
        for child in sorted(directory.iterdir())
        if local_path(child).is_dir()
        for found in planning_directories(child)
    )


def known_batches(
    workspace: Workspace, domain: str, supplied: Optional[Path] = None
) -> Tuple[BatchLocation, ...]:
    domain_name(domain)
    entries = catalog_entries(workspace)
    paths = tuple(
        local_path(Path(path))
        for path, binding in entries.items()
        if binding["domain"] == domain
    )
    paths += planning_directories(workspace.path("plans/" + domain))
    if supplied is not None:
        paths += (local_path(supplied),)
    result = []
    for path in dict.fromkeys(paths):
        batch = batch_location(path)
        binding = entries.get(str(path))
        if binding is not None and binding != batch.binding:
            raise MigrationError(
                "Batch configuration differs from its registered identity: " + str(path)
            )
        if supplied == path and batch.manifest["domain"] != domain:
            raise MigrationError("The selected plan belongs to another domain")
        if batch.manifest["domain"] == domain:
            result.append(batch)
    return tuple(result)


def require_batch_ownership(
    batches: Tuple[BatchLocation, ...], directory: Path
) -> None:
    conflicts = tuple(
        file
        for batch in batches
        if batch.directory != directory
        for file in batch.operations
    )
    if conflicts:
        raise MigrationError(
            "Another batch has operation journals for this domain. Reconcile its source holds and target changes; "
            "keep these records:\n" + "\n".join(str(file) for file in conflicts)
        )


def register_batch(directory: Path, workspace: Optional[Workspace] = None) -> None:
    workspace = workspace or Workspace()
    batch = batch_location(directory)
    entries = catalog_entries(workspace)
    existing = entries.get(str(batch.directory))
    require_batch_ownership(
        known_batches(workspace, batch.manifest["domain"], batch.directory),
        batch.directory,
    )
    if existing is None:
        workspace.write(
            "batch-catalog.json",
            {
                "version": 1,
                "kind": "migration-batch-catalog",
                "batches": {**entries, str(batch.directory): batch.binding},
            },
        )
