import json
from pathlib import Path
from typing import Any, Mapping, Optional

from . import SourceConnection
from ..core.domain import domain_name
from ..core.errors import MigrationError
from ..core.storage import decode_json
from ..resources import read_resource


def read_plesk(connection: SourceConnection, action: str, domain: str, diagnostic_file: Optional[Path] = None) -> Any:
    domain_name(domain)
    if action not in ("inspect", "export"):
        raise MigrationError("Unsupported Plesk read operation")
    return plesk_request(connection, "source", {"action": action, "domain": domain}, diagnostic_file)


def plesk_request(
    connection: SourceConnection, helper: str, request: Mapping[str, Any], diagnostic_file: Optional[Path] = None,
) -> Any:
    if helper not in ("source", "isolation"):
        raise MigrationError("Unsupported Plesk helper")
    parts = ("common", "source") if helper == "source" else ("common", "forwarding", "isolation")
    script = "\n".join(read_resource("plesk-" + part + ".py") for part in parts)
    output = connection.ssh.run(
        ("python3", "-c", script), input_text=json.dumps(request, allow_nan=False),
        timeout=300, diagnostic_file=diagnostic_file,
    ).stdout
    try:
        return decode_json(output)
    except ValueError as error:
        raise MigrationError("Invalid Plesk response; potentially private output was not printed") from error
