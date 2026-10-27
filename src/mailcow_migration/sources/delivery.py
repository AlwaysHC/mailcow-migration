import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

from ..core.errors import MigrationError
from ..core.state import require_identity
from ..core.storage import local_path, read_json, write_private_json
from ..plans.saved import SavedPlan
from . import SourceKind
from .php import source_php

HOLD = "retry:4.3.2 Domain migration in progress"
TRANSPORT_MAP = "/etc/postfix/mailcow-migration-transport"
BACKUPS = "/root/mailcow-in-progress/delivery-backups/"
BACKUP_PATH = re.compile(re.escape(BACKUPS) + r"(?:handoff-)?[a-f0-9]{64}\.json")


@dataclass(frozen=True)
class DeliverySnapshot:
    transport: Mapping[str, Any] = field(repr=False)
    routing: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True)
class SourceDelivery:
    batch: SavedPlan
    api: str

    def __post_init__(self) -> None:
        if (
            self.batch.source.kind is not SourceKind.POSTFIXADMIN
            or not self.batch.source.mail_root
        ):
            raise MigrationError(
                "This delivery step supports PostfixAdmin; Plesk requires its separate isolation adapter"
            )
        if self.batch.plan["domains"] != [self.batch.plan["domain"]]:
            raise MigrationError(
                "Source routing requires one primary domain; linked domains need a recipient-mapping adapter"
            )
        request = self.routing_request
        if any(
            not re.fullmatch(r"[a-zA-Z0-9_.+%@-]+", value)
            for value in (*self.keys, *request["recipients"], *request["rejected"])
        ):
            raise MigrationError(
                "Unsupported source delivery address; no route was guessed"
            )

    @property
    def keys(self) -> Tuple[str, ...]:
        return tuple(
            sorted(
                {
                    *self.batch.plan["domains"],
                    *(row["username"] for row in self.batch.plan["mailboxes"]),
                }
            )
        )

    @property
    def file(self) -> Path:
        return local_path(self.batch.directory / "source-delivery.json")

    @property
    def identity(self) -> Mapping[str, Any]:
        return {
            "version": 1,
            "kind": "postfixadmin-delivery-isolation",
            "plan": self.batch.fingerprint,
            "source": asdict(self.batch.source),
            "api": self.api,
            "keys": list(self.keys),
            "map": TRANSPORT_MAP,
            "source_isolated": False,
            "webmail_sessions_verified": False,
        }

    @property
    def routing_request(self) -> Mapping[str, Any]:
        plan = self.batch.plan
        recipients = sorted(
            {
                row["address"]
                for row in plan["aliases"]
                if row["active"] and not row["address"].startswith("@")
            }
        )
        catch_all = any(
            row["address"] == "@" + plan["domain"] and row["active"]
            for row in plan["aliases"]
        )
        rejected = (
            []
            if catch_all
            else [
                row["address"]
                for row in plan["aliases"]
                if not row["active"] and not row["address"].startswith("@")
            ]
        )
        entry = {
            "plan": self.batch.fingerprint,
            "api": self.api,
            "route": HOLD,
            "recipients": recipients,
            "catchAll": catch_all,
        }
        return {
            "domain": plan["domain"],
            "isolation": True,
            "entry": entry,
            "keys": self.keys,
            "recipients": recipients,
            "rejected": rejected,
            "catchAll": catch_all,
        }

    def record(self) -> Optional[Mapping[str, Any]]:
        if not self.file.exists():
            return None
        record = read_json(self.file, private=True)
        if not isinstance(record, dict):
            raise MigrationError("Invalid source delivery journal")
        require_identity(record, self.identity)
        if (
            type(record.get("complete")) is not bool
            or not isinstance(record.get("stage"), str)
            or not isinstance(record.get("backup"), str)
            or not BACKUP_PATH.fullmatch(record["backup"])
            or not isinstance(record.get("backups"), list)
            or record["backup"] not in record["backups"]
            or any(
                not isinstance(value, str) or not BACKUP_PATH.fullmatch(value)
                for value in record["backups"]
            )
        ):
            raise MigrationError(
                "Invalid source delivery recovery record; retain it before further changes"
            )
        return record

    def request(
        self, routing: bool, action: str, fingerprint: Optional[str] = None
    ) -> Mapping[str, Any]:
        if (
            action not in ("inspect", "check", "apply")
            or action == "apply"
            and not re.fullmatch(r"[a-f0-9]{64}", fingerprint or "")
        ):
            raise MigrationError(
                "Unsupported source delivery operation or missing preview fingerprint"
            )
        root, domain = self.batch.source.mail_root, self.batch.plan["domain"]
        parameters = (
            self.routing_request
            if routing
            else {
                "keys": self.keys,
                "roots": [root + "/" + domain, root + "/sieve/" + domain],
            }
        )
        result = source_php(
            self.batch.source,
            self.batch.directory,
            "postfixadmin-routing.php" if routing else "postfixadmin-delivery.php",
            {
                **parameters,
                "action": action,
                **({"fingerprint": fingerprint} if fingerprint is not None else {}),
            },
            shared=("postfixadmin-commands.php",),
            timeout=300,
        )
        if action == "inspect":
            if not isinstance(result.get("fingerprint"), str) or not re.fullmatch(
                r"[a-f0-9]{64}", result["fingerprint"]
            ):
                raise MigrationError(
                    "Source delivery preview did not return a valid configuration fingerprint"
                )
        elif action == "check":
            if result:
                raise MigrationError(
                    "Unexpected source delivery verification response; contents omitted"
                )
        else:
            backup = (
                BACKUPS + ("handoff-" if routing else "") + str(fingerprint) + ".json"
            )
            if result.get("backup") != backup and not (
                routing
                and result.get("alreadyComplete") is True
                and result.get("backup") is None
            ):
                raise MigrationError(
                    "Source delivery write did not confirm its backup; retain the journal before retrying"
                )
            if routing and result.get("route") != HOLD:
                raise MigrationError(
                    "Source recipient routing did not confirm the delivery hold; retain its recovery records"
                )
        return result

    def inspect(self) -> DeliverySnapshot:
        self.record()
        routing = self.request(True, "inspect")
        if (
            routing.get("routes") != list(self.keys)
            or type(routing.get("complete")) is not bool
            or routing.get("queue") is not None
            or not isinstance(routing.get("originalRecipientSetting"), str)
            or not isinstance(routing.get("bccMaps"), list)
            or any(not isinstance(value, str) for value in routing["bccMaps"])
        ):
            raise MigrationError(
                "Invalid source recipient-routing preview; contents omitted"
            )
        transport = self.request(False, "inspect")
        if (
            not isinstance(transport.get("chain"), str)
            or not isinstance(transport.get("entries"), dict)
            or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in transport["entries"].items()
            )
            or not isinstance(transport.get("blockers"), list)
            or any(not isinstance(value, str) for value in transport["blockers"])
        ):
            raise MigrationError(
                "Invalid source delivery-map inventory; contents omitted"
            )
        if transport["blockers"]:
            raise MigrationError(
                "Source delivery isolation blocked:\n"
                + "\n".join(transport["blockers"])
            )
        for key in self.keys:
            if transport["entries"].get(key) and transport["entries"][key] != HOLD:
                raise MigrationError("Source migration route conflicts for " + key)
        return DeliverySnapshot(transport, routing)

    def verify(self) -> None:
        self.request(False, "check")
        self.request(True, "check")

    def check(self) -> None:
        record = self.record()
        if not record or not record["complete"]:
            raise MigrationError(
                "No completed source delivery pause to check; resume the saved operation first"
            )
        self.verify()

    def pause(self, snapshot: DeliverySnapshot) -> None:
        previous = self.record()
        backup = BACKUPS + snapshot.transport["fingerprint"] + ".json"
        state = {
            **self.identity,
            "backup": backup,
            "backups": list(
                dict.fromkeys([*(previous["backups"] if previous else []), backup])
            ),
            "complete": False,
        }
        stage = "apply delivery hold"

        def checkpoint(following: str) -> None:
            nonlocal stage
            stage = following
            write_private_json(self.file, {**state, "stage": stage}, replace=True)

        checkpoint(stage)
        print(f"Source delivery operation journal: {self.file}")
        try:
            self.request(False, "apply", snapshot.transport["fingerprint"])
            print("Postfix delivery held; previous delivery workers drained.")
            routing = self.request(True, "inspect")
            routing_backup = BACKUPS + "handoff-" + routing["fingerprint"] + ".json"
            state = {
                **state,
                "backups": list(dict.fromkeys([*state["backups"], routing_backup])),
            }
            checkpoint("pause recipient expansion")
            self.request(True, "apply", routing["fingerprint"])
            print(
                "Domain alias, vacation and BCC expansion paused; pending recipients checked."
            )
            checkpoint("verify delivery isolation")
            self.verify()
            write_private_json(
                self.file,
                {**state, "stage": "complete", "complete": True},
                replace=True,
            )
        except (MigrationError, OSError) as error:
            raise MigrationError(
                "{}\nSource delivery holds may already be installed. Stopped while attempting to {}. "
                "Retain {} and the backups on source {}: {}. Rerun after resolving the blocker; "
                "do not overwrite other batches' routes or requeue all mail.".format(
                    error,
                    stage,
                    self.file,
                    self.batch.source.ssh.host,
                    ", ".join(state["backups"]),
                )
            ) from error
