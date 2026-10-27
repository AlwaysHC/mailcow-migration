import base64
import hashlib
import json
import shlex
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
from uuid import uuid4

from ..core.errors import MigrationError
from ..core.storage import decode_json, write_private_json
from ..resources import php_resource
from . import SourceConnection


def source_php(
    connection: SourceConnection,
    directory: Path,
    helper: str,
    request: Mapping[str, Any],
    *,
    shared: Sequence[str] = (),
    timeout: float = 120,
) -> Mapping[str, Any]:
    script = "\n".join(php_resource(name) for name in (*shared, helper))
    result = connection.ssh.run(
        ("php", "-d", "display_errors=stderr", "-r", script),
        input_text=json.dumps(request, allow_nan=False),
        timeout=timeout,
        diagnostic_file=directory / (Path(helper).stem + "-" + uuid4().hex + ".txt"),
    )
    return php_response(result.stdout)


def php_response(
    output: str, *, diagnostic_directory: Optional[Path] = None
) -> Mapping[str, Any]:
    try:
        response = decode_json(output)
        if not isinstance(response, dict):
            raise TypeError("Expected an object")
    except (TypeError, ValueError, UnicodeError) as error:
        if diagnostic_directory is not None:
            content = output.encode("utf-8")
            file = diagnostic_directory / (
                "export-output-" + hashlib.sha256(content).hexdigest() + ".json"
            )
            write_private_json(
                file, {"bytes": len(content), "output": output}, replace=True
            )
            raise MigrationError(
                f"Invalid source PHP response; exact output saved privately to {file}"
            ) from error
        raise MigrationError("Invalid source PHP response; contents omitted") from error
    return response


def source_php_file(
    connection: SourceConnection,
    directory: Path,
    helper: str,
    request: Mapping[str, Any],
) -> Mapping[str, Any]:
    content = "<?php\n" + php_resource(helper)
    encoded = base64.b64encode(
        json.dumps(request, allow_nan=False).encode("utf-8")
    ).decode("ascii")
    php = shlex.join(
        (
            "php",
            "-d",
            "display_errors=stderr",
            "-d",
            "html_errors=0",
            "-d",
            "error_reporting=32767",
        )
    )
    prerequisite = (
        'if (PHP_VERSION_ID < 70300 || !extension_loaded("pdo_mysql")) { '
        'fwrite(STDERR, "Roundcube export requires CLI PHP 7.3+ with pdo_mysql on the source.\\n"); exit(1); } '
        'if (file_get_contents($argv[1], false, null, 0, 5) !== "<?php") { '
        'fwrite(STDERR, "Transferred Roundcube exporter has an invalid PHP opening tag; no database access attempted.\\n"); exit(1); }'
    )
    command = "\n".join(
        (
            "set -eu",
            "umask 077",
            "migration_export=$(mktemp /tmp/mailcow-roundcube-XXXXXXXX)",
            "trap 'rm -f -- \"$migration_export\"' 0",
            "trap 'exit 1' HUP INT TERM",
            'cat > "$migration_export"',
            php
            + " -r "
            + shlex.quote(prerequisite)
            + ' -- "$migration_export" </dev/null',
            php + ' "$migration_export" "$1" </dev/null',
        )
    )
    result = connection.ssh.run(
        ("sh", "-c", command, "sh", encoded),
        input_text=content,
        timeout=300,
        diagnostic_file=directory / (Path(helper).stem + "-" + uuid4().hex + ".txt"),
    )
    return php_response(result.stdout, diagnostic_directory=directory)
