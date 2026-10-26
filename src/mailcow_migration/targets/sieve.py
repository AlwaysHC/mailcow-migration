import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

from ..core.errors import MigrationError
from ..core.storage import json_fingerprint, local_path
from ..plans.evidence import ExportEvidence
from ..plans.sieve_evidence import load_sieve_evidence
from ..sources import SourceKind
from .storage import MailStorage


def read_sieve_file(path: Path, root: Path) -> Optional[bytes]:
    path, root = local_path(path), local_path(root)
    if root not in path.parents or not root.is_dir():
        raise MigrationError("Sieve files must remain inside a regular source/target directory")
    for current in (path, *path.parents):
        if current == root:
            break
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if not (stat.S_ISREG(mode) if current == path else stat.S_ISDIR(mode)):
            raise MigrationError("Sieve path must use regular files and directories: " + str(current))
    return path.read_bytes() if path.exists() else None


@dataclass(frozen=True)
class SieveSource:
    files: Mapping[str, Optional[Path]]
    scripts: Mapping[str, Optional[bytes]] = field(repr=False)
    default_file: Optional[Path]
    default_content: Optional[bytes] = field(repr=False)

    @classmethod
    def read(cls, plan: Mapping[str, Any], evidence: ExportEvidence) -> "SieveSource":
        if evidence.source.connection.kind is SourceKind.PLESK:
            source = load_sieve_evidence(plan, evidence)
            return cls(dict.fromkeys(source.scripts), {user: script.encode("utf-8") if script is not None else None
                                                      for user, script in source.scripts.items()}, None, None)
        files = {row["username"]: evidence.data / "sieve" / plan["domain"] / row["payload"]["local_part"] / ".dovecot.sieve"
                 for row in plan["mailboxes"]}
        default = evidence.data / "config/default.sieve"
        return cls(files, {name: read_sieve_file(path, evidence.data) for name, path in files.items()},
                   default, read_sieve_file(default, evidence.data))

    def fingerprint(self) -> str:
        try:
            return json_fingerprint({
                "scripts": {name: content.decode("utf-8") if content is not None else None for name, content in self.scripts.items()},
                "defaultSieve": self.default_content.decode("utf-8") if self.default_content is not None else None,
            }, canonical=True)
        except UnicodeError as error:
            raise MigrationError("Exported Sieve must contain valid UTF-8; retain the original files") from error


@dataclass(frozen=True)
class SieveScript:
    username: str
    source: Optional[Path]
    target: Path
    binary: Path
    content: Optional[bytes] = field(repr=False)
    previous: Optional[bytes] = field(repr=False)
    compiled: Optional[bytes] = field(repr=False)

    @property
    def conflict(self) -> bool:
        return self.previous is not None and self.previous != self.content or self.content is None and self.compiled is not None

    def check_unchanged(self, exported: Path, root: Path) -> None:
        if (self.source is not None and read_sieve_file(self.source, exported) != self.content
                or read_sieve_file(self.target, root) != self.previous
                or read_sieve_file(self.binary, root) != self.compiled):
            raise MigrationError(self.username + ": Sieve files changed after preflight; retain the journal and backups")


def sieve_scripts(
    plan: Mapping[str, Any], evidence: ExportEvidence, storage: MailStorage, *, use_default: bool, replace: bool,
) -> Tuple[Tuple[SieveScript, ...], str]:
    root = local_path(storage.root / "sieve")
    if not root.is_dir():
        raise MigrationError("Expected the existing Mailcow Sieve directory: " + str(root))
    source = SieveSource.read(plan, evidence)
    scripts = []
    for row in plan["mailboxes"]:
        username = row["username"]
        path, content = source.files[username], source.scripts[username]
        if use_default and content is None:
            path, content = source.default_file, source.default_content
            if content is None:
                raise MigrationError("Exported default Sieve is missing")
        target, binary = root / (username + ".sieve"), root / (username + ".svbin")
        script = SieveScript(username, path, target, binary, content, read_sieve_file(target, root), read_sieve_file(binary, root))
        if script.conflict and not replace:
            raise MigrationError(username + ": existing Sieve differs; preview with --replace-sieve to back up and reconcile it")
        scripts.append(script)
    return tuple(scripts), source.fingerprint()
