import json
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4

from . import SourceConnection
from ..core.errors import MigrationError
from ..core.storage import decode_json
from ..resources import php_resource


def source_php(
    connection: SourceConnection, directory: Path, helper: str, request: Mapping[str, Any], *,
    shared: Sequence[str] = (), timeout: float = 120,
) -> Mapping[str, Any]:
    script = "\n".join(php_resource(name) for name in (*shared, helper))
    result = connection.ssh.run(
        ("php", "-d", "display_errors=stderr", "-r", script), input_text=json.dumps(request, allow_nan=False), timeout=timeout,
        diagnostic_file=directory / (Path(helper).stem + "-" + uuid4().hex + ".txt"),
    )
    try:
        response = decode_json(result.stdout)
    except (ValueError, UnicodeError) as error:
        raise MigrationError("Invalid source PHP response; contents omitted") from error
    if not isinstance(response, dict):
        raise MigrationError("Expected a source PHP response object; contents omitted")
    return response
