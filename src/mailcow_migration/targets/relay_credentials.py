import re
import tarfile
from pathlib import Path
from typing import Mapping

from ..core.errors import MigrationError
from ..core.postfix import postfix_settings


def relay_credentials(directory: Path, postfix: str) -> Mapping[str, Mapping[str, str]]:
    settings = postfix_settings(postfix)
    if settings.get("smtp_sasl_auth_enable") != "yes":
        return {}
    match = re.fullmatch(
        r"(?:hash|btree|lmdb|cdb|texthash):(/etc/postfix/[a-zA-Z0-9_./-]+)",
        settings.get("smtp_sasl_password_maps", ""),
    )
    if match is None or ".." in Path(match[1]).parts:
        raise MigrationError(
            "Source relay authentication needs a supported password-map adapter"
        )
    try:
        with tarfile.open(directory / "server-config.tgz", "r:gz") as archive:
            members = tuple(
                member for member in archive.getmembers() if member.name == match[1][1:]
            )
            if (
                len(members) != 1
                or not members[0].isfile()
                or members[0].size > 8 * 1024 * 1024
            ):
                raise MigrationError(
                    "Source relay password map is missing, duplicated or not a regular archive member"
                )
            stream = archive.extractfile(members[0])
            if stream is None:
                raise MigrationError("Cannot read the source relay password map")
            with stream:
                text = stream.read().decode("utf-8")
    except (tarfile.TarError, UnicodeError) as error:
        raise MigrationError(
            "Invalid relay credential archive; contents omitted"
        ) from error
    credentials = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        entry = re.fullmatch(r"(\S+)\s+([^\s:]+):([^\s]+)\s*", line)
        if entry is None or entry[1] in credentials:
            raise MigrationError(
                "Unsupported or duplicate source relay password-map entry; credentials omitted"
            )
        credentials[entry[1]] = {"username": entry[2], "password": entry[3]}
    return credentials
