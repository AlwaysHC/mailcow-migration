from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from .reconciliation import api_number, api_string
from .sieve_runtime import SieveRuntime, same_script
from .sogo import SogoTarget, missing_user_sources, require_username
from .sogo_filters import SogoFilterImport
from .sogo_values import enabled, enabled_setting, equal_values, preference_equal, preference_pair, property_list
from ..configuration import Configuration
from ..core.errors import ConfigurationError, MigrationError
from ..core.state import require_identity
from ..core.storage import json_fingerprint, local_path, read_json, write_private_json
from ..core.timezones import timezone_parts
from ..plans.roundcube_values import MISSING, source_truth
from ..plans.saved import SavedPlan
from ..plans.saved_webmail import SavedWebmail, load_saved_webmail
from ..plans.sieve import requires_forwarding_prefilter
from ..prefilters import prefilter_activations, prefilter_staging, validate_prefilter_journal
from ..transfers import TransferContext, require_mailcow_host


def handoff_prefilter(
    user: Mapping[str, Any], expected: Optional[Mapping[str, Any]], inventory: Sequence[Mapping[str, Any]],
) -> Optional[Mapping[str, Any]]:
    username = user["username"]
    if user.get("prefilter") is not None and expected is None:
        raise MigrationError(username + ": forwarding prefilter has no staging record")
    rows = [row for row in inventory if row.get("username") == username and row.get("filter_type") == "prefilter"]
    owned = [row for row in rows if expected is not None and all(row.get(key) == expected[key] for key in ("script_desc", "script_data"))]
    if (expected is not None and (len(owned) != 1 or type(owned[0].get("id")) not in (str, int) or not api_string(owned[0]["id"]))
            or any(api_number(row.get("active")) not in (0, 1) or api_number(row.get("active")) != 0 and row not in owned for row in rows)):
        raise MigrationError(username + ": existing prefilter cannot be reconciled with the staging journal")
    return owned[0] if owned else None


def prefilter_values(row: Mapping[str, Any]) -> Mapping[str, Any]:
    number = api_number(row.get("active"))
    return {"active": int(number) if number in (0, 1) else number,
            "script_data": row.get("script_data"), "script_desc": row.get("script_desc")}


@dataclass(frozen=True)
class SogoSettingsImport:
    filters: SogoFilterImport
    configuration: Configuration

    @classmethod
    def from_export(cls, configuration: Configuration, batch: SavedPlan, directory: Path) -> "SogoSettingsImport":
        return cls(SogoFilterImport.from_export(configuration, batch, directory), configuration)

    @property
    def target(self) -> SogoTarget:
        return SogoTarget(self.filters.target.target, local_path(self.filters.batch.directory / "sogo-settings"))

    @property
    def scripts(self) -> SieveRuntime:
        return SieveRuntime(self.target.target, self.target.directory)

    @property
    def file(self) -> Path:
        return local_path(self.target.directory / "state.json")

    def state(self, reviewed: SavedWebmail) -> Mapping[str, Any]:
        users = {user["username"]: user for user in reviewed.proposal["nativeSettings"]["users"]}
        identity = {"version": 1, "kind": "sogo-settings-import", "plan": self.filters.batch.fingerprint,
                    "api": self.target.target.settings.api, "mailcow_directory": self.target.target.settings.directory,
                    "export_id": self.filters.evidence.manifest["export_id"], "import_complete": False,
                    "source_sha256": json_fingerprint({"users": list(users.values()), "timeZone": reviewed.policy["vacationTimeZone"],
                                                       "filters": reviewed.proposal["users"]}, canonical=True)}
        if not self.file.exists():
            return {**identity, "users": {}, "settings_complete": False}
        saved = read_json(self.file, private=True)
        if (not isinstance(saved, dict) or type(saved.get("version")) is not int or saved.get("import_complete") is not False
                or type(saved.get("settings_complete")) is not bool or not isinstance(saved.get("users"), dict)):
            raise MigrationError("Invalid SOGo settings journal; retain " + str(self.file))
        require_identity(saved, identity)
        for username, record in saved["users"].items():
            if (username not in users or not isinstance(record, dict) or record.get("preferences") != users[username]["preferences"]
                    or record.get("prefilter") != users[username].get("prefilter") or not isinstance(record.get("script"), str)):
                raise MigrationError("Invalid native SOGo settings receipt; retain " + str(self.file))
        if "pending" in saved:
            username = saved["pending"]
            if not isinstance(username, str) or username not in users or username in saved["users"] or saved["settings_complete"]:
                raise MigrationError("Invalid pending native SOGo settings write; retain " + str(self.file))
            raise MigrationError("Uncertain SOGo settings write for {}; reconcile {} and its backups before retrying".format(username, self.file))
        if saved["settings_complete"] and saved["users"].keys() != users.keys():
            raise MigrationError("Native SOGo settings completion lacks all user receipts; retain " + str(self.file))
        return saved

    def save(self, state: Mapping[str, Any]) -> Mapping[str, Any]:
        write_private_json(self.file, state, replace=True)
        return state

    def staged_filters(self, reviewed: SavedWebmail) -> Mapping[str, Any]:
        journal, operations = prefilter_staging(TransferContext(self.filters.batch, self.configuration), self.filters.evidence, reviewed)
        if not journal.workspace.path(journal.relative_path).exists():
            return {}
        state = journal.read()
        if state.get("pending") or state.get("complete") is not True:
            raise MigrationError("Complete forwarding/vacation prefilter staging before native settings import")
        validate_prefilter_journal(state, operations, prefilter_activations(reviewed, state))
        return {row["username"]: row for row in state["filters"]}

    def inspect(
        self, user: Mapping[str, Any], source: Mapping[str, Any], staged: Mapping[str, Any], dates: Mapping[int, Mapping[str, str]], zone: str,
    ) -> Mapping[str, Any]:
        username = user["username"]
        preferences = preference_pair(self.target.backup(username), username)
        current = preferences[0]
        filters = current.get("SOGoSieveFilters")
        if not isinstance(filters, list) or not preference_equal("SOGoSieveFilters", filters, source["filters"]):
            raise MigrationError(username + ": editable filters changed since export; reconcile before generating the native delivery script")
        forwarding = user["preferences"].get("Forward")
        if forwarding and forwarding["keepCopy"] and requires_forwarding_prefilter(filters):
            raise MigrationError(username + ": personal filters conflict with native forwarding's explicit INBOX copy")
        for key, value in user["preferences"].items():
            before = current.get(key, MISSING)
            if source_truth(before) and isinstance(before, (dict, list, str)) and len(before) and not preference_equal(key, before, value):
                raise MigrationError(username + ": existing SOGo " + key + " settings conflict; reconcile before importing")
        for key in ("Vacation", "Forward", "Notification"):
            if key not in user["preferences"] and enabled_setting(current, key):
                raise MigrationError(username + ": reconcile enabled target " + key + " before regenerating its script")
        config = self.target.preflight([{**user, "filters": filters}])
        if missing_user_sources([user], config):
            raise MigrationError(username + ": prepare the SOGo user source before native settings import")
        vacation = user["preferences"].get("Vacation")
        for seconds in (vacation["startDate"], vacation["endDate"]) if vacation else ():
            expected = "{year}-{month}-{day} {hour}:{minute}:{second}".format(**dates[seconds])
            for command in (self.target.command, self.scripts.command):
                if command(("date", "-d", "@" + str(seconds), "+%F %T")).strip() != expected:
                    raise MigrationError(username + ": container timezone differs from vacationTimeZone (" + zone + ")")
        active = self.scripts.active(username)
        if not same_script(active, self.scripts.stored(username)) or not same_script(active, source["script"]):
            raise MigrationError(username + ": active/stored Sieve differs from the export; no overwrite performed")
        prefilter = handoff_prefilter(user, staged.get(username), self.target.target.api.inventory("filters"))
        proposed = [{**current, **user["preferences"]}, preferences[1]]
        property_list({"preferences": proposed}, xml=True)
        return {"preferences": preferences, "active": active, "prefilter": prefilter, "proposed": proposed}

    def verify_user(
        self, user: Mapping[str, Any], preferences: list, prefilter: Optional[Mapping[str, Any]],
        dates: Mapping[int, Mapping[str, str]],
    ) -> str:
        username = user["username"]
        if not equal_values(preference_pair(self.target.backup(username), username), preferences):
            raise MigrationError(username + ": native SOGo settings readback differs; retain the pending journal")
        if not self.scripts.sogo_status(username)[1]:
            raise MigrationError(username + ": SOGo did not activate its generated script")
        generated = self.scripts.stored(username)
        if not same_script(generated, self.scripts.active(username)):
            raise MigrationError(username + ": generated SOGo script is not active")
        if enabled_setting(user["preferences"], "Vacation"):
            for key, operator in (("startDate", "ge"), ("endDate", "le")):
                date = "{year}-{month}-{day}".format(**dates[user["preferences"]["Vacation"][key]])
                if 'currentdate :value "{}" "date" "{}"'.format(operator, date) not in generated:
                    raise MigrationError(username + ": generated vacation script does not enforce the imported date window")
        if prefilter is not None:
            rows = [row for row in self.target.target.api.inventory("filters") if api_string(row.get("id")) == api_string(prefilter["id"])]
            if (len(rows) != 1 or rows[0].get("username") != username or rows[0].get("filter_type") != "prefilter"
                    or prefilter_values(rows[0]) != prefilter_values(prefilter)):
                raise MigrationError(username + ": Mailcow prefilter differs from the planned handoff; retain the pending journal")
        return generated

    def run(
        self, *, verify_ready: Callable[[], None], check_source: Callable[[], None], apply: bool = False, writers_stopped: bool = False,
    ) -> None:
        """Caller holds the workspace lock; verify_ready checks final mail/Sieve, activation and source/target isolation."""
        require_mailcow_host()
        if apply and not writers_stopped:
            raise ConfigurationError("Native SOGo settings import requires explicit stopped-writer confirmation")
        self.filters.evidence.verify()
        sources = {user["username"]: user for user in self.filters.users()}
        reviewed = load_saved_webmail(self.filters.batch, self.filters.evidence)
        if reviewed.blockers:
            raise MigrationError("Native SOGo settings import is blocked:\n" + "\n".join(reviewed.blockers))
        users = reviewed.proposal["nativeSettings"]["users"]
        for user in users:
            require_username(user["username"])
        state = self.state(reviewed)
        if state["settings_complete"]:
            print("Native SOGo settings were already imported and verified; subsequent target user edits are retained.")
            return
        verify_ready()
        check_source()
        pending = [user for user in users if user["username"] not in state["users"]]
        staged = self.staged_filters(reviewed) if pending else {}
        if pending:
            if self.filters.state(list(sources.values()))["filters_complete"] is not True:
                raise MigrationError("Finish editable SOGo filter import before native vacation/forwarding settings")
            self.scripts.preflight()
            if any(user["preferences"] for user in pending):
                self.target.command(("test", "-r", "/etc/sogo/sieve.creds"))
        zone = reviewed.policy["vacationTimeZone"]
        dates = timezone_parts(zone, [user["preferences"]["Vacation"][key] for user in pending if "Vacation" in user["preferences"]
                                      for key in ("startDate", "endDate")])
        for user in pending:
            self.inspect(user, sources[user["username"]], staged, dates, zone)
            print(user["username"] + ": native vacation/forwarding settings and prefilter handoff passed preflight.")
        self.filters.evidence.verify()
        check_source()
        if not apply:
            print("Native settings preview completed; private backups retained, no preferences, scripts or prefilters changed.")
            return
        state = self.save(state)
        print("Private native SOGo settings journal: {}.".format(self.file))
        for user in pending:
            username = user["username"]
            before = self.inspect(user, sources[username], staged, dates, zone)
            preferences, existing = before["proposed"], before["prefilter"]
            prefilter = ({**existing, **user.get("prefilter", {"active": 0})} if existing is not None else None)
            write_private_json(self.target.directory / ("before-" + json_fingerprint(username) + ".json"), before, replace=True)
            state = self.save({**state, "pending": username})
            if prefilter is not None and prefilter_values(existing) != prefilter_values(prefilter):
                verify_ready()
                self.target.target.api.request("edit/filter", {"items": [api_string(existing["id"])], "attr": prefilter_values(prefilter)})
            if user["preferences"]:
                verify_ready()
                self.target.restore_preferences(username, preferences)
                if (any(enabled(row.get("active")) for row in preferences[0]["SOGoSieveFilters"])
                        or any(enabled_setting(preferences[0], key) for key in ("Vacation", "Forward"))):
                    verify_ready()
                    self.target.set_user_value("defaults", username, "SOGoSieveFilters", preferences[0]["SOGoSieveFilters"], generate_sieve=True)
            generated = self.verify_user(user, preferences, prefilter, dates)
            record = {"preferences": user["preferences"], "script": generated, **({"prefilter": user["prefilter"]} if "prefilter" in user else {})}
            state = self.save({**{key: value for key, value in state.items() if key != "pending"}, "users": {**state["users"], username: record}})
            print(username + ": native settings, active script and applicable prefilter handoff verified.")
        self.filters.evidence.verify()
        self.state(load_saved_webmail(self.filters.batch, self.filters.evidence))
        check_source()
        verify_ready()
        self.save({**state, "settings_complete": True, "completed_at": datetime.now(timezone.utc).isoformat()})
        print("Native SOGo settings import completed; full webmail completion and target release remain separate.")
