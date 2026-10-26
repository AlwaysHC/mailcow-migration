from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .sogo import missing_user_sources
from .sogo_folders import expanded_folders, inspect_folder_state, mail_settings, primary_folder
from .sogo_inventory import check_book, check_preferences
from .sogo_preparation import SogoPreparation
from .sogo_values import equal_values, preference_equal
from ..core.errors import MigrationError
from ..core.state import require_identity
from ..core.storage import json_fingerprint, write_private_json
from ..plans.roundcube_values import MISSING


@dataclass(frozen=True)
class SogoImport:
    preparation: SogoPreparation

    @property
    def file(self) -> Path:
        return self.preparation.file("native-import-state.json")

    @property
    def identity(self) -> Mapping[str, Any]:
        return {"version": 1, "kind": "sogo-native-import", **self.preparation.identity,
                "proposal_sha256": json_fingerprint(self.preparation.proposal, canonical=True), "import_complete": False}

    def state(self) -> Mapping[str, Any]:
        saved = self.preparation.document(self.file.name, "sogo-native-import")
        if saved is None:
            return {**self.identity, "users": {}, "native_complete": False}
        require_identity(saved, self.identity)
        if (not isinstance(saved.get("users"), dict) or type(saved.get("native_complete")) is not bool
                or saved.get("import_complete") is not False):
            raise MigrationError("Invalid native SOGo import journal; retain " + str(self.file))
        users = {user["username"]: user for user in self.preparation.users}
        for username, record in saved["users"].items():
            if username not in users or not isinstance(record, dict):
                raise MigrationError("Unknown user in native SOGo import journal; retain " + str(self.file))
            user = users[username]
            preferences = record.get("preferences")
            if (record.get("book") != user["book"] or not isinstance(preferences, dict)
                    or any(key not in user["preferences"] or value not in ("pending", "complete") for key, value in preferences.items())
                    or type(record.get("pending")) is not bool or type(record.get("complete")) is not bool
                    or type(record.get("count")) is not int or not 0 <= record["count"] <= len(user["records"])):
                raise MigrationError(username + ": invalid native import receipt; retain " + str(self.file))
            if record["complete"] and (record["pending"] or record["count"] != len(user["records"])
                                       or any(preferences.get(key) != "complete" for key in user["preferences"])):
                raise MigrationError(username + ": incomplete native import was marked complete; retain its journal")
            folders = record.get("folders")
            if folders is not None:
                plan = user["folderState"]
                if (plan is None or not isinstance(folders, dict) or folders.get("source") != plan["source"]
                        or type(folders.get("complete")) is not bool or not isinstance(folders.get("expected"), list)
                        or not isinstance(folders.get("mail"), dict) or "ExpandedFolders" not in folders["mail"]):
                    raise MigrationError(username + ": invalid folder-state import receipt; retain its journal")
                expected = expanded_folders(folders["expected"])
                expanded = expanded_folders(folders["mail"]["ExpandedFolders"])
                if (expected != sorted(set(expected)) or "/0" not in expected or not all(map(primary_folder, expected))
                        or sorted({value for value in expanded if primary_folder(value)}) != expected):
                    raise MigrationError(username + ": invalid saved folder expansion; retain its journal")
        if saved["native_complete"] and (saved["users"].keys() != users.keys() or any(
                not saved["users"][name]["complete"] or user["folderState"] is not None
                and not (saved["users"][name].get("folders") or {}).get("complete") for name, user in users.items())):
            raise MigrationError("Native SOGo completion lacks all user receipts; retain " + str(self.file))
        return saved

    def save(self, state: Mapping[str, Any]) -> Mapping[str, Any]:
        write_private_json(self.file, state, replace=True)
        return state

    def save_user(self, state: Mapping[str, Any], username: str, record: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.save({**state, "users": {**state["users"], username: record}, "native_complete": False})

    def import_user(
        self, state: Mapping[str, Any], user: Mapping[str, Any], present: list, verify_ready: Callable[[], None],
    ) -> Mapping[str, Any]:
        target, username = self.preparation.target, user["username"]
        journal = state["users"].get(username, {"book": user["book"], "preferences": {}, "pending": False,
                                                "complete": False, "count": 0, "folders": None})
        for key, value in user["preferences"].items():
            current = check_preferences(target.backup(username), user, target.directory, journal)
            if not preference_equal(key, current.get(key, MISSING), value):
                verify_ready()
                journal = {**journal, "complete": False, "preferences": {**journal["preferences"], key: "pending"}}
                state = self.save_user(state, username, journal)
                target.set_user_value("defaults", username, key, value)
                verified = check_preferences(target.backup(username), user, target.directory, journal)
                if not preference_equal(key, verified.get(key, MISSING), value):
                    raise MigrationError(username + ": SOGo did not retain " + key + "; import journal retained")
            journal = {**journal, "preferences": {**journal["preferences"], key: "complete"}}
            state = self.save_user(state, username, journal)
        retained = set(present)
        missing = [row for row in user["records"] if row["c_name"] not in retained]
        for offset in range(0, len(missing), 50):
            chunk = missing[offset:offset + 50]
            verify_ready()
            journal = {**journal, "pending": True, "complete": False}
            state = self.save_user(state, username, journal)
            check_book(target.backup(username), username, user["book"], user["records"], owned=True)
            target.restore_contacts(username, user["book"], chunk)
            verified = set(check_book(target.backup(username), username, user["book"], user["records"], owned=True))
            expected = retained | {row["c_name"] for row in chunk}
            if not expected <= verified:
                raise MigrationError(username + ": SOGo did not retain all expected contacts; journal retained for reconciliation")
            retained = expected
        state = self.save_user(state, username, {**journal, "pending": False, "complete": True, "count": len(user["records"])})
        print(username + ": contacts and preferences imported and read back.")
        return state

    def import_folders(
        self, state: Mapping[str, Any], user: Mapping[str, Any], prepared: Mapping[str, Any], verify_ready: Callable[[], None],
    ) -> Mapping[str, Any]:
        target, username = self.preparation.target, user["username"]
        journal = state["users"][username]
        current = inspect_folder_state(target.target, target.directory, user, target.backup(username), journal.get("folders"))
        if current["target"] != prepared["target"]:
            raise MigrationError(username + ": target folder tree changed after preflight; rerun before importing folder state")
        folders = {"source": current["source"], "expected": current["expected"], "mail": current["proposed_mail"], "complete": False}
        verify_ready()
        state = self.save_user(state, username, {**journal, "folders": folders})
        if current["write_required"]:
            target.set_user_value("settings", username, "Mail", folders["mail"])
            if not equal_values(mail_settings(target.backup(username), username), folders["mail"]):
                raise MigrationError(username + ": SOGo did not retain the imported Mail settings; journal retained")
        state = self.save_user(state, username, {**journal, "folders": {**folders, "complete": True}})
        print(username + ": SOGo folder expansion imported and read back.")
        return state

    def apply(self, *, verify_ready: Callable[[], None], check_source: Callable[[], None]) -> None:
        """Caller holds the workspace lock; verify_ready checks transfer, activation and source/target isolation, raising on failure."""
        state = self.state()
        if state["native_complete"]:
            print("Native SOGo import was already completed; subsequent target user edits are retained.")
            return
        verify_ready()
        check_source()
        config = self.preparation.preflight()
        missing = missing_user_sources(self.preparation.users, config)
        if missing:
            raise MigrationError("Prepare SOGo user sources before importing data: " + ", ".join(missing))
        target = self.preparation.target
        inventories = {}
        for user in self.preparation.users:
            username = user["username"]
            journal = state["users"].get(username)
            target.clear_unknown_user(username)
            backup = target.backup(username)
            check_preferences(backup, user, target.directory, journal)
            folders = inspect_folder_state(target.target, target.directory, user, backup, journal.get("folders") if journal else None)
            present = check_book(backup, username, user["book"], user["records"], owned=journal is not None)
            inventories[username] = {"contacts": present, "folders": folders}
        print("Native SOGo import journal: {}".format(self.file))
        for user in self.preparation.users:
            state = self.import_user(state, user, inventories[user["username"]]["contacts"], verify_ready)
        for user in self.preparation.users:
            folders = inventories[user["username"]]["folders"]
            if folders is not None:
                state = self.import_folders(state, user, folders, verify_ready)
        check_source()
        verify_ready()
        self.save({**state, "native_complete": True})
        print("Native SOGo data import completed. This receipt does not authorize target release or complete the migration.")
