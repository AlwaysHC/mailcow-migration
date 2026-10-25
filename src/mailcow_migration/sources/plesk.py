import json
from pathlib import Path
from typing import Any, Optional

from . import SourceConnection
from ..core.domain import domain_name
from ..core.errors import MigrationError
from ..core.storage import decode_json
from ..resources import read_resource


def read_plesk(connection: SourceConnection, action: str, domain: str, diagnostic_file: Optional[Path] = None) -> Any:
    domain_name(domain)
    if action not in ("inspect", "export"):
        raise MigrationError("Unsupported Plesk read operation")
    output = connection.ssh.run(
        ("python3", "-c", read_resource("plesk-source.py")), input_text=json.dumps({"action": action, "domain": domain}),
        timeout=300, diagnostic_file=diagnostic_file,
    ).stdout
    try:
        return decode_json(output)
    except ValueError as error:
        raise MigrationError("Invalid Plesk response; potentially private output was not printed") from error
