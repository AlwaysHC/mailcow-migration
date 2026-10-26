import json
from pathlib import Path
from typing import Any, Mapping, Tuple
from uuid import uuid4

from .mailcow import MailcowTarget
from ..core.errors import MigrationError
from ..core.storage import decode_json
from ..plans.passwords import password_schemes
from ..resources import php_resource


def verify_target_password(target: MailcowTarget, kind: str, payload: Mapping[str, Any], password: str, directory: Path) -> bool:
    if kind not in ("mailbox", "domain-admin", "admin"):
        return False
    users = payload.get("items") or ["{}@{}".format(payload["local_part"], payload["domain"]) if kind == "mailbox" else payload["username"]]
    return verify_target_passwords(target, kind, dict.fromkeys(users, password), directory)


def verify_target_passwords(target: MailcowTarget, kind: str, passwords: Mapping[str, str], directory: Path) -> bool:
    if kind not in ("mailbox", "domain-admin", "admin") or not passwords or any(
        not isinstance(user, str) or not user or not isinstance(password, str) or not password for user, password in passwords.items()
    ):
        raise MigrationError("Invalid password verification request; contents omitted")
    result = target.compose(
        ("exec", "-T", "php-fpm-mailcow", "php", "-r", php_resource("verify-password.php")),
        input_text=json.dumps({"kind": kind, "passwords": passwords}, allow_nan=False),
        diagnostic_file=directory / ("password-reconciliation-" + uuid4().hex + ".txt"),
    )
    try:
        verified = decode_json(result.stdout)
    except (ValueError, UnicodeError) as error:
        raise MigrationError("Invalid password verification response; contents omitted") from error
    if type(verified) is not bool:
        raise MigrationError("Password verification did not return a boolean")
    return verified


def check_password_support(plan: Mapping[str, Any], target: MailcowTarget) -> Tuple[str, ...]:
    required = password_schemes(plan)
    if required:
        available = target.compose(("exec", "-T", "dovecot-mailcow", "doveadm", "pw", "-l")).stdout.split()
        missing = tuple(scheme for scheme in required if scheme not in available)
        if missing:
            raise MigrationError("Target Dovecot lacks required password schemes: " + ", ".join(missing))
    return required


def verify_mailbox_passwords(plan: Mapping[str, Any], target: MailcowTarget, directory: Path) -> None:
    check_password_support(plan, target)
    passwords = {row["username"]: row["payload"]["password"] for row in plan["mailboxes"]}
    if passwords and not verify_target_passwords(target, "mailbox", passwords, directory):
        raise MigrationError("Stored mailbox passwords differ from the plan; retain the batch and reconcile without resetting passwords")
