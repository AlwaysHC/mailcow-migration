from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from .mailcow import MailcowTarget
from .sieve_runtime import SieveRuntime, same_script
from .sogo import SogoTarget, missing_user_sources
from .sogo_values import enabled_setting, equal_values, preference_equal, preference_pair, property_list
from ..configuration import Configuration
from ..core.errors import ConfigurationError, MigrationError
from ..core.state import require_identity
from ..core.storage import json_fingerprint, local_path, read_json, write_private_json
from ..plans.evidence import ExportEvidence, load_plan_export
from ..plans.native_settings import mailbox_sieve
from ..plans.roundcube_values import MISSING
from ..plans.saved import SavedPlan
from ..plans.sieve import SieveMappingError
from ..plans.sieve_evidence import load_sieve_evidence, sieve_inventory_blockers
from ..sources import SourceKind
from ..transfers import require_mailcow_host


def other_preferences(preferences: list) -> list:
    return [{key: value for key, value in preferences[0].items() if key != "SOGoSieveFilters"}, preferences[1]]


@dataclass(frozen=True)
class SogoFilterImport:
    target: SogoTarget
    batch: SavedPlan
    evidence: ExportEvidence = field(repr=False)

    @classmethod
    def from_export(cls, configuration: Configuration, batch: SavedPlan, directory: Path) -> "SogoFilterImport":
        evidence = load_plan_export(configuration, batch, directory)
        target = SogoTarget(MailcowTarget(configuration.mailcow), local_path(batch.directory / "sogo-filters"))
        return cls(target, batch, evidence)

    @property
    def file(self) -> Path:
        return local_path(self.target.directory / "state.json")

    @property
    def scripts(self) -> SieveRuntime:
        return SieveRuntime(self.target.target, self.target.directory)

    def users(self) -> list:
        if self.evidence.source.connection.kind is not SourceKind.POSTFIXADMIN:
            raise MigrationError("Editable SOGo filter import supports PostfixAdmin; retain Plesk scripts on their copy/compile path")
        evidence = load_sieve_evidence(self.batch.plan, self.evidence)
        blockers = sieve_inventory_blockers(evidence)
        if blockers:
            raise MigrationError("Cannot import editable SOGo filters:\n" + "\n".join(blockers))
        users = []
        for mailbox in self.batch.plan["mailboxes"]:
            username = mailbox["username"]
            try:
                users.append(mailbox_sieve(username, evidence))
            except SieveMappingError as error:
                raise MigrationError(username + ": cannot import editable SOGo filters: " + str(error) + ". Original script retained.") from error
        return users

    def state(self, users: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        identity = {"version": 1, "kind": "sogo-filter-import", "plan": self.batch.fingerprint,
                    "api": self.target.target.settings.api, "mailcow_directory": self.target.target.settings.directory,
                    "export_id": self.evidence.manifest["export_id"], "source_sha256": json_fingerprint(users, canonical=True),
                    "import_complete": False}
        if not self.file.exists():
            return {**identity, "users": {}, "filters_complete": False}
        saved = read_json(self.file, private=True)
        if (not isinstance(saved, dict) or type(saved.get("version")) is not int or saved.get("import_complete") is not False
                or type(saved.get("filters_complete")) is not bool or not isinstance(saved.get("users"), dict)):
            raise MigrationError("Invalid SOGo filter import journal; retain " + str(self.file))
        require_identity(saved, identity)
        selected = {user["username"] for user in users}
        for username, record in saved["users"].items():
            if username not in selected or not isinstance(record, dict) or record.get("status") not in ("pending", "complete"):
                raise MigrationError("Invalid SOGo filter user receipt; retain " + str(self.file))
            preference_pair(record, username)
        if saved["filters_complete"] and (saved["users"].keys() != selected or any(
                record["status"] != "complete" for record in saved["users"].values())):
            raise MigrationError("SOGo filter completion lacks all user receipts; retain " + str(self.file))
        return saved

    def save(self, state: Mapping[str, Any]) -> Mapping[str, Any]:
        write_private_json(self.file, state, replace=True)
        return state

    def save_user(self, state: Mapping[str, Any], username: str, record: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.save({**state, "users": {**state["users"], username: record}, "filters_complete": False})

    def verify_stored(self, user: Mapping[str, Any]) -> None:
        if not same_script(self.scripts.stored(user["username"]), user["script"]):
            raise MigrationError(user["username"] + ": existing sogo script differs from the import; reconcile before continuing")

    def inspect(self, user: Mapping[str, Any], record: Optional[Mapping[str, Any]] = None) -> Mapping[str, Any]:
        username = user["username"]
        if not same_script(self.scripts.active(username), user["script"]):
            raise MigrationError(username + ": active Sieve differs from the export; no overwrite performed")
        stored, active = self.scripts.sogo_status(username)
        if stored:
            self.verify_stored(user)
        preferences = preference_pair(self.target.backup(username), username)
        current = preferences[0].get("SOGoSieveFilters", MISSING)
        matches = preference_equal("SOGoSieveFilters", current, user["filters"])
        if current is not MISSING and current != [] and not matches:
            raise MigrationError(username + ": existing editable SOGo filters conflict with the import; no overwrite performed")
        for key in ("Vacation", "Forward", "Notification"):
            if enabled_setting(preferences[0], key):
                raise MigrationError(username + ": enabled SOGo " + key + " must be reconciled before replacing its script")
        if record is not None:
            if not equal_values(other_preferences(preferences), other_preferences(record["preferences"])):
                raise MigrationError(username + ": other SOGo preferences changed during import; retain its backups and journal")
            if record["status"] == "complete" and (not matches or not stored or not active):
                raise MigrationError(username + ": completed editable filters changed during the unfinished batch; no write repeated")
        proposed = [{**preferences[0], "SOGoSieveFilters": user["filters"]}, preferences[1]]
        property_list({"preferences": proposed}, xml=True)
        return {"preferences": preferences, "stored": stored, "active": active, "proposed": proposed, "write_required": not matches}

    def run(
        self, *, verify_ready: Callable[[], None], check_source: Callable[[], None], apply: bool = False, writers_stopped: bool = False,
    ) -> None:
        """Caller holds the workspace lock; verify_ready checks final mail/Sieve, activation and source/target isolation."""
        require_mailcow_host()
        if apply and not writers_stopped:
            raise ConfigurationError("SOGo filter import requires explicit stopped-writer confirmation")
        self.evidence.verify()
        users = self.users()
        state = self.state(users)
        if state["filters_complete"]:
            print("Editable SOGo filters were already imported and verified; subsequent target user edits are retained.")
            return
        verify_ready()
        check_source()
        missing = missing_user_sources(users, self.target.preflight(users))
        if missing:
            raise MigrationError("Prepare SOGo user sources before importing editable filters: " + ", ".join(missing))
        self.scripts.preflight()
        for user in users:
            self.inspect(user, state["users"].get(user["username"]))
            print("{}: {} editable SOGo filters passed preflight.".format(user["username"], len(user["filters"])))
        self.evidence.verify()
        check_source()
        if not apply:
            print("Editable-filter preview completed; private backups retained, no preferences or Sieve scripts changed.")
            return
        write_private_json(self.target.directory / "source-filters.json", users, replace=True)
        state = self.save(state)
        print("Private SOGo filter import journal: {}.".format(self.file))
        for user in users:
            username = user["username"]
            record = state["users"].get(username)
            before = self.inspect(user, record)
            if record is not None and record["status"] == "complete":
                continue
            record = {"status": "pending", "preferences": record["preferences"] if record else before["preferences"]}
            state = self.save_user(state, username, record)
            if not before["stored"]:
                verify_ready()
                self.scripts.put(username, user["script"])
                self.verify_stored(user)
            if before["write_required"]:
                verify_ready()
                self.target.restore_preferences(username, before["proposed"])
            verified = preference_pair(self.target.backup(username), username)
            if (not preference_equal("SOGoSieveFilters", verified[0].get("SOGoSieveFilters", MISSING), user["filters"])
                    or not equal_values(other_preferences(verified), other_preferences(record["preferences"]))):
                raise MigrationError(username + ": SOGo preference readback differs; retain the pending import and backups")
            if not before["active"]:
                verify_ready()
                self.scripts.activate(username)
            completed = {**record, "status": "complete"}
            self.inspect(user, completed)
            state = self.save_user(state, username, completed)
            print(username + ": editable filters saved and active script verified.")
        self.evidence.verify()
        check_source()
        verify_ready()
        for user in users:
            self.inspect(user, state["users"][user["username"]])
        self.save({**state, "filters_complete": True, "completed_at": datetime.now(timezone.utc).isoformat()})
        print("Editable SOGo filter import completed; native vacation/forwarding settings and full webmail completion remain separate.")
