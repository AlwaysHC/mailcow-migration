import base64
import binascii
import re
from typing import Any, Mapping, Tuple

from ..core.errors import MigrationError

CRYPT_SCHEMES = (
    (r"\$1\$[^$]{1,8}\$[./a-zA-Z0-9]{22}", "MD5-CRYPT"),
    (r"\$5\$(?:rounds=[0-9]+\$)?[^$]{1,16}\$[./a-zA-Z0-9]{43}", "SHA256-CRYPT"),
    (r"\$6\$(?:rounds=[0-9]+\$)?[^$]{1,16}\$[./a-zA-Z0-9]{86}", "SHA512-CRYPT"),
    (r"\$2[aby]\$[0-9][0-9]\$[./a-zA-Z0-9]{53}", "BLF-CRYPT"),
)


def password_scheme(password: Any, username: str, allowed: Tuple[str, ...]) -> str:
    match = (
        re.fullmatch(r"\{([A-Z0-9-]+)\}(.+)", password)
        if isinstance(password, str)
        else None
    )
    if (
        match is not None
        and match[1] in allowed
        and not re.search(r"[\r\n\0]", password)
    ):
        scheme, value = match.groups()
        if any(
            name == scheme and re.fullmatch(pattern, value)
            for pattern, name in CRYPT_SCHEMES
        ):
            return scheme
        if scheme == "SSHA256":
            try:
                if len(base64.b64decode(value, validate=True)) == 48:
                    return scheme
            except (ValueError, binascii.Error) as error:
                raise MigrationError(
                    "Invalid salted password hash for " + username
                ) from error
    raise MigrationError("Unsupported or malformed password hash for " + username)


def password_schemes(plan: Mapping[str, Any]) -> Tuple[str, ...]:
    kind = plan["source"].get("kind", "postfixadmin")
    if kind not in ("postfixadmin", "plesk"):
        raise MigrationError("Unknown password source adapter")
    allowed = (
        ("MD5-CRYPT",)
        if kind == "postfixadmin"
        else (*(name for _, name in CRYPT_SCHEMES), "SSHA256")
    )
    schemes = []
    for row in plan["mailboxes"]:
        payload = row["payload"]
        schemes.append(
            password_scheme(payload.get("password"), row["username"], allowed)
        )
        if (
            payload.get("password2") != payload["password"]
            or payload.get("force_pw_update") != 0
        ):
            raise MigrationError(
                "Mailbox password payload would differ or require a reset: "
                + row["username"]
            )
    return tuple(sorted(set(schemes)))
