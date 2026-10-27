import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from ..configuration import Configuration
from ..core.errors import MigrationError
from ..core.state import require_identity
from ..core.storage import local_path, read_json, write_private_json
from ..plans.saved import SavedPlan, validate_plan_inventory
from .inventory import inspect_source
from .logins import SourceLogins
from .php import source_php


@dataclass(frozen=True)
class SourceSubmission:
    batch: SavedPlan
    configuration: Configuration

    @property
    def file(self) -> Path:
        return local_path(self.batch.directory / "source-submission.json")

    @property
    def identity(self) -> Mapping[str, Any]:
        return {
            "version": 1,
            "kind": "postfixadmin-submission-isolation",
            "plan": self.batch.fingerprint,
            "source": asdict(self.batch.source),
            "api": self.configuration.mailcow.api,
            "mailcow_directory": self.configuration.mailcow.directory,
        }

    def record(self) -> Optional[Mapping[str, Any]]:
        if not self.file.exists():
            return None
        state = read_json(self.file, private=True)
        if (
            not isinstance(state, dict)
            or state.get("complete") is not True
            or not isinstance(state.get("submission"), dict)
        ):
            raise MigrationError(
                "Invalid source-submission receipt; retain " + str(self.file)
            )
        require_identity(state, self.identity)
        return state

    def inspect(self) -> Mapping[str, Any]:
        previous = self.record()
        validate_plan_inventory(
            self.batch.plan,
            inspect_source(self.batch.source, self.batch.plan["domain"]).inventory,
        )
        snapshot = source_php(
            self.batch.source,
            self.batch.directory,
            "postfixadmin-submission.php",
            {"action": "inspect"},
            shared=("postfixadmin-commands.php",),
        )
        if (
            snapshot.get("backend") != "dovecot"
            or not isinstance(snapshot.get("fingerprint"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", snapshot["fingerprint"])
            or not isinstance(snapshot.get("sockets"), list)
            or not snapshot["sockets"]
            or any(
                not isinstance(path, str) or not path.startswith("/")
                for path in snapshot["sockets"]
            )
        ):
            raise MigrationError("Invalid source SMTP authentication inspection")
        if previous and previous["submission"] != snapshot:
            raise MigrationError(
                "Source submission configuration changed after isolation; retain its receipt and reconcile"
            )
        return snapshot

    def check_authentication(self) -> None:
        logins = SourceLogins(self.batch)
        if logins.users:
            logins.check()
        for user in logins.users:
            result = logins.command(
                ("doveadm", "auth", "lookup", "-x", "service=smtp", "-f", "user", user),
                allowed_codes=(0, 67),
            )
            if result.returncode != 67:
                raise MigrationError(
                    "Source SMTP authentication remains available for " + user
                )

    def complete(self, snapshot: Mapping[str, Any]) -> None:
        if self.inspect() != snapshot:
            raise MigrationError(
                "Source submission configuration changed during isolation"
            )
        self.check_authentication()
        write_private_json(
            self.file,
            {**self.identity, "complete": True, "submission": snapshot},
            replace=True,
        )

    def check(self) -> None:
        if self.record() is None:
            raise MigrationError(
                "Complete source-delivery --action pause --apply after the login freeze to record SMTP verification"
            )
        self.inspect()
        self.check_authentication()
