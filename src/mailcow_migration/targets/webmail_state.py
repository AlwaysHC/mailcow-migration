import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

from ..configuration import MailcowConfiguration
from ..core.errors import MigrationError
from ..core.state import require_identity
from ..core.storage import json_fingerprint, local_path, read_json, write_private_json
from ..plans.saved import SavedPlan
from ..sources import SourceKind

RECEIPTS = {
    "roundcube": ("sogo-target/native-import-state.json", "sogo-native-import", "native_complete"),
    "filters": ("sogo-filters/state.json", "sogo-filter-import", "filters_complete"),
    "settings": ("sogo-settings/state.json", "sogo-settings-import", "settings_complete"),
}
INPUTS = frozenset(("export_id", "export_sha256", "source_sha256", "proposal_sha256", "webmail_sha256"))


@dataclass(frozen=True)
class WebmailJournal:
    batch: SavedPlan
    target: MailcowConfiguration

    @property
    def file(self) -> Path:
        return local_path(self.batch.directory / "roundcube/state.json")

    @property
    def identity(self) -> Mapping[str, Any]:
        return {"version": 1, "kind": "mailcow-webmail-import", "plan": self.batch.fingerprint, "source": asdict(self.batch.source),
                "api": self.target.api, "mailcow_directory": self.target.directory}

    @property
    def receipt_names(self) -> Tuple[str, ...]:
        return ("roundcube",) if self.batch.source.kind is SourceKind.PLESK else tuple(RECEIPTS)

    def read(self, inputs: Optional[Mapping[str, str]] = None) -> Optional[Mapping[str, Any]]:
        if not self.file.exists():
            return None
        state = read_json(self.file, private=True)
        if (not isinstance(state, dict) or type(state.get("version")) is not int or type(state.get("complete")) is not bool
                or state.get("stage") not in (*self.receipt_names, "verify", "complete") or not isinstance(state.get("inputs"), dict)
                or state["inputs"].keys() != INPUTS or any(not isinstance(value, str) or not value
                                                         for value in state["inputs"].values())
                or any(not re.fullmatch(r"[a-f0-9]{64}", value) for key, value in state["inputs"].items() if key != "export_id")
                or not isinstance(state.get("receipts"), dict) or state["receipts"].keys() - set(self.receipt_names)
                or any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value) for value in state["receipts"].values())
                or type(state.get("deferred_preferences")) is not int or state["deferred_preferences"] < 0):
            raise MigrationError("Invalid webmail import journal; retain " + str(self.file))
        require_identity(state, self.identity)
        if inputs is not None and state["inputs"] != inputs:
            raise MigrationError("Webmail export or approved proposal changed after import started; retain the journals and reconcile")
        if state["complete"] and (state["stage"] != "complete" or state["receipts"].keys() != set(self.receipt_names)):
            raise MigrationError("Webmail completion lacks all native import receipts")
        return state

    def write(self, inputs: Mapping[str, str], stage: str, receipts: Mapping[str, str], deferred: int) -> None:
        previous = self.read(inputs)
        if previous is not None and previous["complete"]:
            raise MigrationError("Completed webmail import cannot be restarted; subsequent target user edits must be retained")
        state = {**self.identity, "inputs": inputs, "stage": stage, "complete": stage == "complete",
                 "receipts": receipts, "deferred_preferences": deferred}
        if stage == "complete":
            state = {**state, "completed_at": datetime.now(timezone.utc).isoformat()}
        write_private_json(self.file, state, replace=True)

    def receipt(self, name: str) -> str:
        if name not in self.receipt_names:
            raise MigrationError("Webmail stage does not belong to this source adapter")
        relative, kind, complete = RECEIPTS[name]
        state = read_json(local_path(self.batch.directory / relative), private=True)
        if (not isinstance(state, dict) or type(state.get("version")) is not int or state.get(complete) is not True
                or state.get("pending") or state.get("import_complete") is not False):
            raise MigrationError("Finish the " + name + " webmail stage before recording completion or releasing Mailcow")
        require_identity(state, {"version": 1, "kind": kind, "plan": self.batch.fingerprint,
                                 "api": self.target.api, "mailcow_directory": self.target.directory})
        return json_fingerprint(state, canonical=True)

    def require_complete(self) -> Mapping[str, Any]:
        state = self.read()
        if state is None or not state["complete"]:
            raise MigrationError("Complete webmail --apply and its verification before releasing Mailcow")
        if any(state["receipts"][name] != self.receipt(name) for name in self.receipt_names):
            raise MigrationError("Webmail stage receipts changed after completion; retain the journals and reconcile before release")
        return state
