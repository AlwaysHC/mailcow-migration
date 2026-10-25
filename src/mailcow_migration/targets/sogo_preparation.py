from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from .sogo import SogoTarget, configuration_blockers, missing_custom_from, missing_user_sources
from .sogo_folders import inspect_folder_state
from .sogo_inventory import check_book, check_preferences
from .sogo_values import configuration_value, enabled, property_list
from ..core.errors import MigrationError
from ..core.state import require_identity
from ..core.storage import json_fingerprint, local_path, read_json, read_text, write_atomic_text, write_private_json, write_private_text
from ..plans.saved import SavedPlan


@dataclass(frozen=True)
class SogoPreparation:
    target: SogoTarget
    batch: SavedPlan
    proposal: Mapping[str, Any] = field(repr=False)

    @property
    def identity(self) -> Mapping[str, Any]:
        return {"plan": self.batch.fingerprint, "api": self.target.target.settings.api,
                "mailcow_directory": self.target.target.settings.directory}

    @property
    def users(self) -> list:
        return self.proposal["users"]

    def file(self, name: str) -> Path:
        return local_path(self.target.directory / name)

    def document(self, name: str, kind: str) -> Optional[Mapping[str, Any]]:
        file = self.file(name)
        if not file.exists():
            return None
        document = read_json(file, private=True)
        if not isinstance(document, dict) or type(document.get("version")) is not int or document["version"] != 1 or document.get("kind") != kind:
            raise MigrationError("Invalid SOGo preparation record: {}".format(file))
        require_identity(document, self.identity)
        return document

    def state(self) -> Optional[Mapping[str, Any]]:
        state = self.document("config-state.json", "sogo-config-state")
        if state is not None and (type(state.get("pending")) is not bool or not isinstance(state.get("before"), str)
                                  or not isinstance(state.get("after"), str) or not isinstance(state.get("domains"), list)
                                  or any(domain not in self.batch.plan["domains"] for domain in state["domains"])):
            raise MigrationError("Invalid pending SOGo configuration change; retain its recovery record")
        return state

    def identity_configuration(self, before: str, domains: list) -> str:
        parsed = self.target.parse(before)
        overrides = parsed.get("domains")
        if overrides is not None and (not isinstance(overrides, dict) or any(not isinstance(value, dict) for value in overrides.values())):
            raise MigrationError("Cannot preserve the persistent SOGo domain overrides")
        changed = {**parsed, "SOGoMailCustomFromEnabled": "YES"}
        if overrides:
            changed["domains"] = {domain: {**values, "SOGoMailCustomFromEnabled": "YES"} if domain in domains else values
                                  for domain, values in overrides.items()}
        after = property_list(changed) + "\n"
        self.target.parse(after)
        return after

    def preview(self) -> Mapping[str, Any]:
        state = self.state()
        pending = state is not None and state["pending"]
        if not self.users and not pending:
            report = {"version": 1, "kind": "sogo-target-assessment", **self.identity, "settings_ready": True,
                      "user_sources_missing": [], "users": [], "user_data_checked": False, "import_ready": False}
            write_private_json(self.file("assessment.json"), report, replace=True)
            print("No selected RoundCube profiles require SOGo preparation.")
            return report
        config = self.target.probe()
        missing = missing_custom_from(self.users, config)
        before = read_text(self.target.configuration_file)
        if pending:
            if before not in (state["before"], state["after"]):
                raise MigrationError("SOGo configuration drift; retain {}".format(self.file("config-state.json")))
            after, domains = state["after"], state["domains"]
        elif missing:
            after, domains = self.identity_configuration(before, missing), missing
        else:
            after, domains = before, []
        preview = {"version": 1, "kind": "sogo-config-preview", **self.identity,
                   "proposal_sha256": json_fingerprint(self.proposal, canonical=True), "file": str(self.target.configuration_file),
                   "before": before, "after": after, "domains": domains, "restart_required": bool(missing or pending)}
        write_private_json(self.file("preparation-preview.json"), preview, replace=True)
        report = {"version": 1, "kind": "sogo-target-assessment", **self.identity,
                  "proposal_sha256": preview["proposal_sha256"], "missing_custom_from": missing,
                  "user_sources_missing": missing_user_sources(self.users, config), "configuration_pending": pending,
                  "blockers": configuration_blockers(self.users, config), "settings_ready": False,
                  "user_data_checked": False, "import_ready": False}
        report = {**report, "settings_ready": not report["blockers"] and not pending}
        write_private_json(self.file("assessment.json"), report, replace=True)
        if preview["restart_required"]:
            print("Would set SOGoMailCustomFromEnabled = YES, preserving other parsed settings and unrelated domain overrides.")
            print("This changes the default for all SOGo users and this batch's affected domain overrides: " + ", ".join(domains))
            print("Applying saves a private backup and restarts sogo-mailcow, briefly interrupting webmail/DAV for all domains.")
        else:
            print("Required SOGo custom-identity settings are already enabled.")
        if report["user_sources_missing"]:
            print("SOGo user sources are not loaded for: " + ", ".join(report["user_sources_missing"]))
            print("Native user-data inspection must wait for valid user sources; inactive staging does not establish import readiness.")
        print("Private SOGo assessment: {}".format(self.file("assessment.json")))
        print("Preview finished without configuration or user-data changes. Native user checks and import readiness remain separate.")
        return report

    def configure(self) -> None:
        state = self.state()
        pending = state is not None and state["pending"]
        if not self.users and not pending:
            print("No selected RoundCube profiles require SOGo configuration changes.")
            return
        config = self.target.probe()
        missing = missing_custom_from(self.users, config)
        if not missing and not pending:
            print("SOGo custom identities are already enabled; no configuration change or restart was needed.")
            return
        before = read_text(self.target.configuration_file)
        if pending:
            if before not in (state["before"], state["after"]):
                raise MigrationError("Interrupted SOGo configuration differs from its saved before/after values; retain the journal")
            original, after, domains = state["before"], state["after"], state["domains"]
        else:
            preview = self.document("preparation-preview.json", "sogo-config-preview")
            if (preview is None or preview.get("proposal_sha256") != json_fingerprint(self.proposal, canonical=True)
                    or preview.get("file") != str(self.target.configuration_file) or preview.get("before") != before
                    or not isinstance(preview.get("after"), str) or not isinstance(preview.get("domains"), list)
                    or any(domain not in self.batch.plan["domains"] for domain in preview["domains"])
                    or preview["domains"] != missing
                    or preview.get("restart_required") is not True):
                raise MigrationError("Run sogo-target preview and review the current configuration change before applying it")
            original, after, domains = before, preview["after"], preview["domains"]
        if after != self.identity_configuration(original, domains):
            raise MigrationError("Saved SOGo configuration change differs from the supported identity-setting update; retain its records")
        backup = self.file("sogo-conf-before-" + json_fingerprint(original) + ".conf")
        if backup.exists():
            if read_text(backup, private=True) != original:
                raise MigrationError("SOGo configuration backup differs from its original contents")
        else:
            write_private_text(backup, original)
        state = {"version": 1, "kind": "sogo-config-state", **self.identity, "before": original, "after": after,
                 "domains": domains, "backup": str(backup), "pending": True}
        write_private_json(self.file("config-state.json"), state, replace=True)
        try:
            if read_text(self.target.configuration_file) != before:
                raise MigrationError("SOGo configuration changed after preview; no replacement attempted")
            if before != after:
                write_atomic_text(self.target.configuration_file, after, replace=True)
            self.target.restart()
            effective = self.target.effective_configuration()
            if any(not enabled(configuration_value(effective, domain, "SOGoMailCustomFromEnabled")) for domain in domains):
                raise MigrationError("SOGo custom identities remain disabled by an overriding configuration")
            write_private_json(self.file("config-state.json"), {**state, "pending": False}, replace=True)
            print("SOGo custom identities enabled and read back successfully.")
        except (MigrationError, OSError) as error:
            raise MigrationError("{}\nSOGo configuration may already be installed. Retain {} and rerun configure --apply "
                                 "after resolving the blocker; do not overwrite intervening configuration changes.".format(
                                     error, self.file("config-state.json"))) from error

    def preflight(self) -> Mapping[str, Any]:
        state = self.state()
        if state is not None and state["pending"]:
            raise MigrationError("Resume the pending SOGo configuration/restart before source isolation or user-data inspection")
        return self.target.preflight(self.users)

    def inspect_users(self) -> bool:
        config = self.preflight()
        missing = missing_user_sources(self.users, config)
        if missing:
            raise MigrationError("SOGo user sources are not loaded for: " + ", ".join(missing)
                                 + ". Finish account activation and reconcile source loading before inspecting user data")
        results = []
        for user in self.users:
            try:
                backup = self.target.backup(user["username"])
                current = check_preferences(backup, user, self.target.directory)
                check_book(backup, user["username"], user["book"], user["records"], owned=False)
                folders = inspect_folder_state(self.target.target, self.target.directory, user, backup)
                results.append({"username": user["username"], "status": "verified",
                                "preferences_missing": [key for key in user["preferences"] if key not in current], "folder_state": folders})
                print(user["username"] + ": existing preferences, destination address book and applicable folder state passed checks.")
            except MigrationError as error:
                results.append({"username": user["username"], "status": "blocked", "detail": str(error)})
                print(user["username"] + ": " + str(error))
        verified = all(row["status"] == "verified" for row in results)
        report = {"version": 1, "kind": "sogo-user-assessment", **self.identity,
                  "proposal_sha256": json_fingerprint(self.proposal, canonical=True), "users": results,
                  "import_ready": False, "target_folder_verification": verified,
                  "folder_users_checked": sum(row.get("folder_state") is not None for row in results)}
        write_private_json(self.file("user-assessment.json"), report, replace=True)
        print("Private user assessment: {}. No user preferences or address books were changed.".format(self.file("user-assessment.json")))
        print("Folder settings are proposed only; user-data import and final readback remain pending.")
        return verified
