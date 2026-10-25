import json
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from .mailcow import MailcowTarget
from ..core.errors import MigrationError
from ..core.storage import decode_json
from ..resources import read_resource


def verify_target_password(target: MailcowTarget, kind: str, payload: Mapping[str, Any], password: str, directory: Path) -> bool:
    if kind not in ("mailbox", "domain-admin", "admin"):
        return False
    users = payload.get("items") or ["{}@{}".format(payload["local_part"], payload["domain"]) if kind == "mailbox" else payload["username"]]
    helper = read_resource("verify-password.php")
    if not helper.startswith("<?php\n"):
        raise MigrationError("Invalid packaged password verifier")
    result = target.compose(
        ("exec", "-T", "php-fpm-mailcow", "php", "-r", helper[6:]),
        input_text=json.dumps({"kind": kind, "users": users, "password": password}, allow_nan=False),
        diagnostic_file=directory / ("password-reconciliation-" + uuid4().hex + ".txt"),
    )
    try:
        verified = decode_json(result.stdout)
    except (ValueError, UnicodeError) as error:
        raise MigrationError("Invalid password verification response; contents omitted") from error
    if type(verified) is not bool:
        raise MigrationError("Password verification did not return a boolean")
    return verified
