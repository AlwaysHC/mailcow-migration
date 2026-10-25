import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Tuple

from .mailcow import MailcowTarget
from ..core.commands import run_command
from ..core.errors import MigrationError
from ..core.storage import decode_json


@dataclass(frozen=True)
class MailStorage:
    container: str
    root: Path
    uid: int
    gid: int

    def document(self) -> Mapping[str, Any]:
        return {"container": self.container, "containerRoot": str(self.root), "uid": self.uid, "gid": self.gid}


def inspect_container(target: MailcowTarget, service: str) -> Tuple[str, Mapping[str, Any]]:
    identifier = target.compose(("ps", "-q", service)).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]+", identifier):
        raise MigrationError("{} container is not running".format(service))
    result = run_command(("docker", "inspect", identifier), cwd=Path(target.settings.directory), label=service + " container inspection")
    try:
        inspection = decode_json(result.stdout)
    except (ValueError, UnicodeError) as error:
        raise MigrationError("Invalid Docker inspection response; contents omitted") from error
    if not isinstance(inspection, list) or not inspection or not isinstance(inspection[0], dict):
        raise MigrationError("Invalid Docker container inspection")
    return identifier, inspection[0]


def mail_volume_root(inspection: Mapping[str, Any]) -> Path:
    mounts = inspection.get("Mounts")
    if not isinstance(mounts, list) or any(not isinstance(mount, dict) for mount in mounts):
        raise MigrationError("Invalid Dovecot mount inventory")
    mount = next((mount for mount in mounts if mount.get("Destination") == "/var/vmail"), None)
    volume = mount.get("Source") if mount is not None else None
    if not isinstance(volume, str) or not Path(volume).is_absolute():
        raise MigrationError("Cannot resolve /var/vmail mount")
    root = Path(volume).resolve(strict=True)
    if not root.is_dir():
        raise MigrationError("Target mail volume is not a directory")
    return root


def inspect_mail_storage(target: MailcowTarget) -> MailStorage:
    identifier, inspection = inspect_container(target, "dovecot-mailcow")
    root = mail_volume_root(inspection)
    identity = []
    for flag in ("-u", "-g"):
        value = target.compose(("exec", "-T", "dovecot-mailcow", "id", flag, "vmail")).stdout.strip()
        if not re.fullmatch(r"[0-9]+", value) or int(value) <= 0:
            raise MigrationError("Invalid vmail identity")
        identity.append(int(value))
    return MailStorage(identifier, root, identity[0], identity[1])
