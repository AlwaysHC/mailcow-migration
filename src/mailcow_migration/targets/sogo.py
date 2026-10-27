import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Sequence
from uuid import uuid4

from .mailcow import MailcowTarget
from .sogo_values import configuration_value, enabled, preference_pair, property_list
from ..core.errors import MigrationError
from ..core.storage import decode_json, json_fingerprint, local_path, read_text, write_private_text
from ..plans.roundcube_values import source_truth
from ..resources import php_resource


def require_username(username: str) -> None:
    if not re.fullmatch(r"[a-zA-Z0-9_.+%-]+@[a-zA-Z0-9.-]+", username):
        raise MigrationError("SOGo operations require a literal mailbox username")


def user_domains(users: Sequence[Mapping[str, Any]]) -> list:
    return list(dict.fromkeys(user["username"].split("@")[1] for user in users))


def missing_custom_from(users: Sequence[Mapping[str, Any]], config: Mapping[str, Any]) -> list:
    return [domain for domain in user_domains([user for user in users if user["preferences"].get("SOGoMailIdentities")])
            if not enabled(configuration_value(config, domain, "SOGoMailCustomFromEnabled"))]


def missing_user_sources(users: Sequence[Mapping[str, Any]], config: Mapping[str, Any]) -> list:
    missing = []
    for domain in user_domains(users):
        sources = configuration_value(config, domain, "SOGoUserSources")
        if not isinstance(sources, list) or not sources:
            missing.append(domain)
    return missing


def configuration_blockers(users: Sequence[Mapping[str, Any]], config: Mapping[str, Any]) -> list:
    blockers = []
    for user in users:
        username, preferences = user["username"], user["preferences"]
        domain = username.split("@")[1]
        setting = lambda key: configuration_value(config, domain, key)
        filters = user.get("filters", [])
        if not isinstance(filters, list):
            raise MigrationError(username + ": invalid editable-filter proposal")
        if filters or any(source_truth(preferences.get(key)) for key in ("Vacation", "Forward")):
            if not enabled(setting("SOGoSieveScriptsEnabled")):
                blockers.append(username + ": SOGo editable filters are disabled")
            for key in ("Vacation", "Forward"):
                if source_truth(preferences.get(key)) and not enabled(setting("SOGo" + key + "Enabled")):
                    blockers.append(username + ": enable SOGo" + key + "Enabled before importing " + key)
            if (source_truth(preferences.get("Vacation")) and setting("SOGoVacationPeriodEnabled") is not None
                    and not enabled(setting("SOGoVacationPeriodEnabled"))):
                blockers.append(username + ": SOGoVacationPeriodEnabled is required to preserve the vacation window")
            if source_truth(preferences.get("Vacation")) or source_truth(preferences.get("Forward")):
                for key in ("SOGoVacationHeaderTemplateFile", "SOGoVacationFooterTemplateFile",
                            "SOGoSieveScriptHeaderTemplateFile", "SOGoSieveScriptFooterTemplateFile"):
                    if source_truth(setting(key)):
                        blockers.append(username + ": reconcile " + key + " before generating native settings")
            if (any(action.get("method") == "redirect" for row in filters for action in row["actions"])
                    and not enabled(setting("SOGoForwardEnabled"))):
                blockers.append(username + ": enable SOGoForwardEnabled before importing editable redirect rules")
        if preferences.get("SOGoMailIdentities") and not enabled(setting("SOGoMailCustomFromEnabled")):
            blockers.append(username + ": SOGoMailCustomFromEnabled is required to preserve sender names and reply-to addresses")
    return blockers


@dataclass(frozen=True)
class SogoTarget:
    target: MailcowTarget
    directory: Path

    @property
    def configuration_file(self) -> Path:
        return local_path(Path(self.target.settings.directory) / "data/conf/sogo/sogo.conf")

    def command(self, arguments: Sequence[str], *, input_text: Optional[str] = None) -> str:
        return self.target.compose(
            ("exec", "-T", "-u", "sogo", "sogo-mailcow", *arguments), input_text=input_text, timeout=300,
            diagnostic_file=self.directory / ("sogo-diagnostics-" + uuid4().hex + ".txt"),
        ).stdout

    def parse(self, text: str) -> Mapping[str, Any]:
        try:
            result = decode_json(self.command(("plutil", "-convert", "json", "-"), input_text=text))
        except (ValueError, UnicodeError) as error:
            raise MigrationError("SOGo returned invalid property-list JSON; contents omitted") from error
        if not isinstance(result, dict):
            raise MigrationError("Expected a SOGo property-list dictionary; contents omitted")
        return result

    def effective_configuration(self) -> Mapping[str, Any]:
        return self.parse(self.command(("sogo-tool", "dump-defaults", "all")))

    def probe(self) -> Mapping[str, Any]:
        self.command(("sh", "-c", 'for tool in sogo-tool plutil; do command -v "$tool" >/dev/null || '
                      '{ printf "Missing required command in sogo-mailcow: %s\\n" "$tool" >&2; exit 1; }; done'))
        if self.parse("{ probe = ready; }").get("probe") != "ready":
            raise MigrationError("SOGo plutil cannot read the required property-list format")
        return self.effective_configuration()

    def preflight(self, users: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        if not users:
            return {}
        config = self.probe()
        blockers = configuration_blockers(users, config)
        if blockers:
            raise MigrationError("SOGo target preparation is incomplete:\n" + "\n".join(blockers))
        return config

    def restart(self) -> None:
        print("Restarting sogo-mailcow; webmail/DAV for all domains is briefly interrupted.")
        self.target.compose(("restart", "sogo-mailcow"), timeout=300,
                            diagnostic_file=self.directory / ("sogo-restart-" + uuid4().hex + ".txt"))
        self.command(("sh", "-c", 'attempt=0; until nc -z 127.0.0.1 20000; do attempt=$((attempt + 1)); '
                      'if [ "$attempt" -gt 30 ]; then printf "%s\\n" '
                      '"SOGo startup timed out after 60 seconds; inspect its startup log." >&2; exit 1; fi; sleep 2; done'))

    @contextmanager
    def native_record(self, username: str, content: Optional[str] = None) -> Iterator[str]:
        require_username(username)
        folder = self.command(("mktemp", "-d", "/tmp/mailcow-contacts-XXXXXXXX")).strip()
        if not re.fullmatch(r"/tmp/mailcow-contacts-[a-zA-Z0-9]+", folder):
            raise MigrationError("Invalid SOGo temporary directory")
        try:
            if content is not None:
                self.command(("sh", "-c", 'umask 077; cat > "$1"', "sh", folder + "/" + username), input_text=content)
            yield folder
            self.command(("rm", "--", folder + "/" + username))
            self.command(("rmdir", "--", folder))
        except (MigrationError, OSError) as error:
            raise MigrationError("{}\nRetain the private SOGo temporary directory in sogo-mailcow for recovery: {}".format(error, folder)) from error

    def backup(self, username: str) -> Mapping[str, Any]:
        with self.native_record(username) as folder:
            self.command(("sogo-tool", "backup", folder, username))
            raw = self.command(("cat", "--", folder + "/" + username))
            record = self.parse(raw)
            if not isinstance(record.get("tables"), dict):
                raise MigrationError("SOGo backup has no folder inventory for " + username)
            original = local_path(self.directory / ("before-" + json_fingerprint(username) + ".plist"))
            if not original.exists():
                write_private_text(original, raw)
            else:
                read_text(original, private=True)
            return record

    def restore_contacts(self, username: str, book: str, records: Sequence[Mapping[str, str]]) -> None:
        require_username(username)
        if not re.fullmatch(r"roundcube-[a-f0-9]{24}", book):
            raise MigrationError("Unsupported migration address-book identifier")
        content = property_list({"tables": {"/Users/{}/Contacts/{}".format(username, book): {
            "displayname": "Roundcube contacts", "acl": {}, "records": list(records),
        }}})
        with self.native_record(username, content) as folder:
            self.command(("sogo-tool", "restore", "-f", "Contacts/" + book, folder, username))

    def set_user_value(self, section: str, username: str, key: str, value: Any, *, generate_sieve: bool = False) -> None:
        if section not in ("defaults", "settings") or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]*", key):
            raise MigrationError("Unsupported SOGo preference section or key")
        if generate_sieve and (section != "defaults" or key != "SOGoSieveFilters"):
            raise MigrationError("Native Sieve generation requires the SOGoSieveFilters defaults key")
        with self.native_record(username, json.dumps({key: value}, ensure_ascii=False, allow_nan=False)) as folder:
            self.command(("sogo-tool", "user-preferences", "set", section, username, key, "-f", folder + "/" + username,
                          *(("-p", "/etc/sogo/sieve.creds", "-F") if generate_sieve else ())))

    def restore_preferences(self, username: str, preferences: list) -> None:
        preference_pair({"preferences": preferences}, username)
        content = '<?xml version="1.0" encoding="UTF-8"?><plist version="1.0">' + property_list({"preferences": preferences}, xml=True) + "</plist>"
        with self.native_record(username, content) as folder:
            self.command(("sogo-tool", "restore", "-p", folder, username))

    def clear_unknown_user(self, username: str) -> str:
        require_username(username)
        host = self.effective_configuration().get("SOGoMemcachedHost")
        if not isinstance(host, str) or not host.strip():
            raise MigrationError("SOGoMemcachedHost is missing; cannot check cached user lookups")
        result = self.target.compose(
            ("exec", "-T", "php-fpm-mailcow", "php", "-r", php_resource("sogo-unknown-user.php")),
            input_text=json.dumps({"host": host, "username": username}, allow_nan=False),
            diagnostic_file=self.directory / ("sogo-user-lookup-" + uuid4().hex + ".txt"),
        ).stdout
        if result not in ("cleared", "unchanged"):
            raise MigrationError("Unexpected SOGo user-cache response; retain the preparation record and diagnostics")
        if result == "cleared":
            print(username + ": cleared SOGo's cached unknown-user lookup; checking the user source again.")
        return result
