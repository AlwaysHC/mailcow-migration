import re
from pathlib import PurePosixPath
from typing import Mapping

from . import SourceKind
from .inventory import SourceInspection
from ..core.domain import domain_name
from ..core.errors import MigrationError


def source_identity(source: SourceInspection) -> Mapping[str, str]:
    return {"host": source.connection.ssh.host, "root": source.inventory.mail_root,
            **({"subdir": "Maildir"} if source.connection.kind is SourceKind.PLESK else {})}


def mailbox_path(source: SourceInspection, domain: str, local_part: str) -> PurePosixPath:
    domain_name(domain)
    if not re.fullmatch(r"[a-zA-Z0-9_+.-]+", local_part) or local_part in (".", ".."):
        raise MigrationError("Mailbox path is unsupported for direct SSH copying; review its local part")
    path = PurePosixPath(source.inventory.mail_root, domain, local_part)
    return path / "Maildir" if source.connection.kind is SourceKind.PLESK else path
