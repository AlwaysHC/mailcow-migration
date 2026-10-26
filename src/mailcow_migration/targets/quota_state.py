import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

from .mailcow import MailcowTarget
from .reconciliation import api_number
from .storage import inspect_container, mail_volume_root
from ..configuration import MailcowConfiguration
from ..core.errors import MigrationError
from ..core.state import require_identity
from ..core.storage import json_fingerprint, local_path, read_json, write_private_json
from ..plans.saved import SavedPlan


def quota_binding(transfer: Mapping[str, Any]) -> Mapping[str, str]:
    root = transfer.get("containerRoot")
    if not isinstance(root, str) or not Path(root).is_absolute():
        raise MigrationError("The completed transfer has no bound Mailcow mail volume")
    return {"containerRoot": root, "transfer_sha256": json_fingerprint(transfer, canonical=True)}


def quota_volume(target: MailcowTarget, root: str) -> None:
    _, inspection = inspect_container(target, "dovecot-mailcow")
    if str(mail_volume_root(inspection)) != root:
        raise MigrationError("Mailcow mail volume differs from the completed transfer; no quota or release operation is allowed")


@dataclass(frozen=True)
class QuotaJournal:
    batch: SavedPlan
    target: MailcowConfiguration

    @property
    def file(self) -> Path:
        return local_path(self.batch.directory / "quota.json")

    @property
    def usernames(self) -> Tuple[str, ...]:
        return tuple(row["username"] for row in self.batch.plan["mailboxes"] if api_number(row["active"]) == 1)

    @property
    def identity(self) -> Mapping[str, Any]:
        return {"version": 1, "kind": "mailcow-quota-recalculation", "plan": self.batch.fingerprint,
                "source": asdict(self.batch.source), "api": self.target.api, "mailcow_directory": self.target.directory}

    def read(self, transfer: Optional[Mapping[str, Any]] = None) -> Optional[Mapping[str, Any]]:
        if not self.file.exists():
            return None
        state = read_json(self.file, private=True)
        if (not isinstance(state, dict) or type(state.get("version")) is not int or type(state.get("complete")) is not bool
                or not isinstance(state.get("completed"), list) or tuple(state["completed"]) != self.usernames[:len(state["completed"])]
                or not isinstance(state.get("transfer_sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", state["transfer_sha256"])
                or not isinstance(state.get("containerRoot"), str) or not Path(state["containerRoot"]).is_absolute()
                or not isinstance(state.get("started_at"), str)):
            raise MigrationError("Invalid quota recalculation journal; retain " + str(self.file))
        require_identity(state, self.identity)
        pending = state.get("pending")
        if (pending is not None and (not isinstance(pending, str) or len(state["completed"]) == len(self.usernames)
                                    or pending != self.usernames[len(state["completed"])])
                or state["complete"] and (pending is not None or tuple(state["completed"]) != self.usernames)):
            raise MigrationError("Quota completion or pending user differs from its recorded progress; retain " + str(self.file))
        if transfer is not None and any(state[key] != value for key, value in quota_binding(transfer).items()):
            raise MigrationError("Transfer evidence changed after quota recalculation started; retain and reconcile its receipt")
        return state

    def begin(self, transfer: Mapping[str, Any]) -> Mapping[str, Any]:
        self.read(transfer)
        return self.save({**self.identity, **quota_binding(transfer),
                          "started_at": datetime.now(timezone.utc).isoformat(), "completed": [], "pending": None, "complete": False})

    def save(self, state: Mapping[str, Any]) -> Mapping[str, Any]:
        write_private_json(self.file, state, replace=True)
        return state

    def require_complete(self, target: MailcowTarget) -> Mapping[str, Any]:
        transfer = read_json(local_path(self.batch.directory / "transfers.json"), private=True)
        if not isinstance(transfer, dict):
            raise MigrationError("Invalid transfer receipt; retain the quota journal and reconcile")
        state = self.read(transfer)
        if state is None or not state["complete"] or state.get("pending") is not None:
            raise MigrationError("Complete quota --apply after activation before webmail import or target release")
        quota_volume(target, state["containerRoot"])
        return state
