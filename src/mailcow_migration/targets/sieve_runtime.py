import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Tuple
from uuid import uuid4

from ..core.errors import MigrationError
from ..core.storage import decode_json
from .mailcow import MailcowTarget
from .sogo import require_username


def same_script(left: str, right: str) -> bool:
    return left.replace("\r\n", "\n").rstrip() == right.replace("\r\n", "\n").rstrip()


@dataclass(frozen=True)
class SieveRuntime:
    target: MailcowTarget
    directory: Path

    def command(
        self, arguments: Sequence[str], *, input_text: Optional[str] = None
    ) -> str:
        return self.target.compose(
            ("exec", "-T", "dovecot-mailcow", *arguments),
            input_text=input_text,
            timeout=300,
            diagnostic_file=self.directory
            / ("sieve-diagnostics-" + uuid4().hex + ".txt"),
        ).stdout

    def preflight(self) -> None:
        if (
            self.command(("doveconf", "-h", "plugin/sieve")).strip()
            != "/var/vmail/sieve/%u.sieve"
        ):
            raise MigrationError(
                "Unsupported active Sieve path; no filter changes performed"
            )

    def active(self, username: str) -> str:
        require_username(username)
        return self.command(("cat", "--", "/var/vmail/sieve/" + username + ".sieve"))

    def sogo_status(self, username: str) -> Tuple[bool, bool]:
        require_username(username)
        lines = self.command(("doveadm", "sieve", "list", "-u", username)).splitlines()
        matches = [
            line for line in lines if re.fullmatch(r"sogo(?:\s+ACTIVE)?\s*", line)
        ]
        if len(matches) > 1:
            raise MigrationError(username + ": ambiguous stored sogo script inventory")
        return bool(matches), any(
            re.fullmatch(r"sogo\s+ACTIVE\s*", line) for line in matches
        )

    def stored(self, username: str) -> str:
        require_username(username)
        raw = self.command(
            ("doveadm", "-f", "json", "sieve", "get", "-u", username, "sogo")
        )
        try:
            records = decode_json(raw)
        except (ValueError, UnicodeError) as error:
            raise MigrationError(
                username + ": invalid doveadm Sieve JSON; contents omitted"
            ) from error
        if (
            not isinstance(records, list)
            or len(records) != 1
            or not isinstance(records[0], dict)
            or not isinstance(records[0].get("sieve script"), str)
        ):
            raise MigrationError(
                username + ": unexpected doveadm Sieve response; contents omitted"
            )
        return records[0]["sieve script"]

    def put(self, username: str, script: str) -> None:
        require_username(username)
        self.command(
            ("doveadm", "sieve", "put", "-u", username, "sogo"), input_text=script
        )

    def activate(self, username: str) -> None:
        require_username(username)
        self.command(("doveadm", "sieve", "activate", "-u", username, "sogo"))
