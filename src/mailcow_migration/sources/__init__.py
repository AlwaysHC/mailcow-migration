from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional, Protocol, Tuple

from ..configuration import Configuration
from ..core.ssh import SshConnection


class SourceKind(str, Enum):
    POSTFIXADMIN = "postfixadmin"
    PLESK = "plesk"


@dataclass(frozen=True)
class SourceConnection:
    kind: SourceKind
    ssh: SshConnection
    mail_root: Optional[str]
    roundcube_directory: Optional[str]
    mysql_option_file: Optional[str] = None


class SourceAdapter(Protocol):
    connection: SourceConnection

    def inspect(self, domain: str) -> Mapping[str, Any]:
        ...

    def export(self, domain: str) -> Mapping[str, Any]:
        ...


def configured_sources(configuration: Configuration) -> Tuple[SourceConnection, ...]:
    return tuple(
        SourceConnection(
            kind, SshConnection(settings.source, settings.ssh_config),
            getattr(settings, "source_mail_root", None), settings.roundcube_directory, getattr(settings, "mysql_option_file", None),
        )
        for kind in SourceKind if (settings := getattr(configuration, kind.value)) is not None
    )
