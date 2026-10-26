import re
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4

from .mailcow import MailcowTarget
from ..core.errors import MigrationError
from ..core.postfix import postfix_settings
from ..core.state import require_identity
from ..core.storage import json_fingerprint, local_path, optional_text, read_json, write_atomic_text, write_private_json, write_private_text

CONTAINER_PREFIX = "/opt/postfix/conf/mailcow-migration"
SENDER_LOOKUP = "check_sender_access hash:" + CONTAINER_PREFIX + "-senders"
BEGIN, END = "# BEGIN mailcow-migration global", "# END mailcow-migration global"


def sender_restrictions(current: str) -> str:
    rules = tuple(item for item in re.split(r"\s*,\s*", current) if item)
    if any(item not in rules for item in ("permit_mynetworks", "permit_sasl_authenticated", "reject_unauth_destination")):
        raise MigrationError("Target recipient restriction order is unsupported for inserting sender rejections")
    index = max(rules.index("permit_mynetworks"), rules.index("permit_sasl_authenticated"))
    if SENDER_LOOKUP in rules:
        if rules.index(SENDER_LOOKUP) <= index:
            raise MigrationError("Existing sender rejection precedes the trusted/authenticated-client exemptions")
        return current
    return ", ".join((*rules[:index + 1], SENDER_LOOKUP, *rules[index + 1:]))


class GlobalPostfix:
    def __init__(self, target: MailcowTarget, rows: Sequence[Mapping[str, Any]], directory: Path):
        self.target, self.rows, self.directory = target, tuple(rows), local_path(directory)
        self.file = local_path(directory / "postfix-state.json")
        self.identity = {"version": 1, "kind": "mailcow-shared-postfix", "api": target.settings.api,
                         "mailcow_directory": target.settings.directory}
        self.journal = read_json(self.file, private=True) if self.file.exists() else {**self.identity, "pending": False, "files": {}}
        if (not isinstance(self.journal, dict) or type(self.journal.get("version")) is not int
                or type(self.journal.get("pending")) is not bool or not isinstance(self.journal.get("files"), dict)):
            raise MigrationError("Invalid shared Postfix journal; retain " + str(self.file))
        require_identity(self.journal, self.identity)
        if self.journal["pending"]:
            raise MigrationError("Interrupted shared Postfix update requires reconciliation using " + str(self.file))
        self.previous = {name: self.command(("postconf", "-h", name))
                         for name in ("relayhost", "smtpd_recipient_restrictions", "smtp_sasl_password_maps")}
        conf = local_path(Path(target.settings.directory) / "data/conf/postfix")
        if not conf.is_dir():
            raise MigrationError("Expected the existing Mailcow Postfix configuration directory")
        extra_file = conf / "extra.cf"
        extra = optional_text(extra_file) or ""
        start, finish = extra.find(BEGIN), extra.find(END)
        if ((start < 0) != (finish < 0) or start >= 0 and (
            finish < start or extra.find(BEGIN, start + 1) >= 0 or extra.find(END, finish + 1) >= 0
        )):
            raise MigrationError("Malformed shared migration block in extra.cf")
        outside = extra if start < 0 else extra[:start] + re.sub(r"^\r?\n", "", extra[finish + len(END):])
        existing = {} if start < 0 else postfix_settings(extra[start + len(BEGIN):finish])
        desired, files = dict(existing), []
        whitelist = tuple(row for row in rows if row.get("kind") == "postscreen-whitelist")
        self.whitelist_map = "cidr:/opt/postfix/conf/custom_postscreen_whitelist.cidr"
        if whitelist:
            chain = self.command(("postconf", "-h", "postscreen_access_list")).replace(",", " ").split()
            if self.whitelist_map not in chain or any(item != "permit_mynetworks" for item in chain[:chain.index(self.whitelist_map)]):
                raise MigrationError("Target Postscreen whitelist is absent or has unsupported preceding checks")
            file = conf / "custom_postscreen_whitelist.cidr"
            before = optional_text(file)
            additions = []
            for row in whitelist:
                value = self.lookup(row["key"], self.whitelist_map).lower() if before is not None else ""
                if value and value not in ("permit", "ok"):
                    raise MigrationError("Target Postscreen whitelist conflicts for " + row["key"])
                if not value:
                    additions.append(row["key"] + " permit")
            text = ((before or "") + ("\n" if before and not before.endswith("\n") else "")
                    + "# Imported migration IP exceptions\n" + "\n".join(additions) + "\n") if additions else before
            files.append({"file": file, "text": text, "map": None, "shared": True})
        relay = next((row for row in rows if row.get("kind") == "relayhost"), None)
        if relay is not None:
            if self.previous["relayhost"] and self.previous["relayhost"] != relay["nexthop"]:
                raise MigrationError("Target relayhost conflicts with the selected source relay")
            desired["relayhost"] = relay["nexthop"]
            credentials = relay["credentials"]
            if credentials["username"]:
                if self.previous["relayhost"] and not existing.get("smtp_sasl_password_maps"):
                    raise MigrationError("Existing target relay authentication has no owned migration record")
                lookup = "hash:" + CONTAINER_PREFIX + "-sasl"
                desired["smtp_sasl_password_maps"] = (self.previous["smtp_sasl_password_maps"]
                    if lookup in self.previous["smtp_sasl_password_maps"].replace(",", " ").split()
                    else lookup + ", " + self.previous["smtp_sasl_password_maps"])
                files.append({"file": conf / "mailcow-migration-sasl", "map": CONTAINER_PREFIX + "-sasl", "shared": False,
                              "text": relay["nexthop"] + " " + credentials["username"] + ":" + credentials["password"] + "\n"})
        senders = tuple(row for row in rows if row.get("kind") == "sender")
        if senders:
            if re.search(r"^smtpd?/(?:inet|pass)/smtpd_recipient_restrictions\s*=", self.command(("postconf", "-P")), re.M):
                raise MigrationError("Target inbound SMTP service overrides recipient restrictions")
            desired["smtpd_recipient_restrictions"] = sender_restrictions(self.previous["smtpd_recipient_restrictions"])
            files.append({"file": conf / "mailcow-migration-senders", "map": CONTAINER_PREFIX + "-senders", "shared": False,
                          "text": "".join(row["key"].lower() + " REJECT\n" for row in senders)})
        unmanaged = postfix_settings(outside)
        if any(key in unmanaged and unmanaged[key] != value for key, value in desired.items()):
            raise MigrationError("Target extra.cf has a conflicting setting outside the migration block")
        if set(desired) - self.previous.keys():
            raise MigrationError("Shared migration block contains unsupported settings")
        if desired:
            files.append({"file": extra_file, "map": None, "shared": True,
                          "text": outside.rstrip() + "\n" + BEGIN + "\n"
                          + "\n".join(key + " = " + value for key, value in desired.items()) + "\n" + END + "\n"})
        self.desired = desired
        self.changes = tuple({**entry, "before": optional_text(entry["file"])} for entry in files)
        for entry in self.changes:
            database = local_path(Path(str(entry["file"]) + ".db"))
            if entry["map"] and database.exists() and (not database.is_file() or entry["before"] is None):
                raise MigrationError("Target map database is unsafe or lacks a corresponding source file")
            known = self.journal["files"].get(str(entry["file"]))
            if not entry["shared"] and entry["before"] != entry["text"] and (
                known is None and entry["before"] is not None or known is not None and json_fingerprint(entry["before"]) != known
            ):
                raise MigrationError("Unowned or changed target configuration: " + str(entry["file"]))

    def command(self, arguments: Sequence[str]) -> str:
        return self.target.compose(("exec", "-T", "postfix-mailcow", *arguments),
                                   diagnostic_file=self.directory / ("shared-postfix-" + uuid4().hex + ".txt")).stdout.strip()

    def lookup(self, key: str, map_name: str) -> str:
        result = self.target.compose(("exec", "-T", "postfix-mailcow", "postmap", "-q", key, map_name), allowed_codes=(0, 1),
                                     diagnostic_file=self.directory / ("shared-lookup-" + uuid4().hex + ".txt"))
        if result.stderr.strip() or result.returncode and result.stdout.strip():
            raise MigrationError("Shared Postfix lookup failed; private diagnostics were retained")
        return result.stdout.strip()

    def verify(self) -> None:
        for key, value in self.desired.items():
            if self.command(("postconf", "-h", key)) != value:
                raise MigrationError("Shared Postfix setting did not verify: " + key)
        for entry in self.changes:
            if optional_text(entry["file"]) != entry["text"]:
                raise MigrationError("Shared Postfix file did not verify: " + str(entry["file"]))
        for row in self.rows:
            kind = row.get("kind")
            if kind == "postscreen-whitelist" and self.lookup(row["key"], self.whitelist_map).lower() not in ("permit", "ok"):
                raise MigrationError("Postscreen whitelist readback failed: " + row["key"])
            if kind == "sender" and self.lookup(row["key"].lower(), "hash:" + CONTAINER_PREFIX + "-senders") != "REJECT":
                raise MigrationError("Sender rejection readback failed: " + row["key"])
            if kind == "relayhost" and row["credentials"]["username"] and self.lookup(
                row["nexthop"], "hash:" + CONTAINER_PREFIX + "-sasl",
            ) != row["credentials"]["username"] + ":" + row["credentials"]["password"]:
                raise MigrationError("Relay credential-map readback failed; credentials omitted")

    def apply(self) -> None:
        altered = tuple(entry for entry in self.changes if entry["before"] != entry["text"])
        effective = any(self.previous[key] != value for key, value in self.desired.items())
        if not altered and not effective:
            self.verify()
            return
        backup = Path(tempfile.mkdtemp(prefix="postfix-backup-", dir=self.directory))
        for entry in altered:
            if entry["before"] is not None:
                write_private_text(backup / entry["file"].name, entry["before"])
        write_private_json(self.file, {**self.journal, "pending": True, "backup": str(backup), "previous": self.previous,
                                      "restore": [{"file": str(entry["file"]), "existed": entry["before"] is not None,
                                                   "map": entry["map"]} for entry in altered]}, replace=True)
        try:
            for entry in altered:
                if optional_text(entry["file"]) != entry["before"]:
                    raise MigrationError("Target file changed after shared-settings preflight: " + str(entry["file"]))
                write_atomic_text(entry["file"], entry["text"], replace=True, private=bool(entry["map"]))
            for entry in self.changes:
                if entry["map"]:
                    self.command(("postmap", "hash:" + entry["map"]))
            for key, value in self.desired.items():
                self.command(("postconf", "-e", key + " = " + value))
            self.command(("postfix", "check"))
            self.command(("postfix", "reload"))
            self.verify()
            write_private_json(self.file, {**self.identity, "pending": False, "backup": str(backup),
                                          "files": {str(entry["file"]): json_fingerprint(entry["text"]) for entry in self.changes}}, replace=True)
        except (MigrationError, OSError, KeyboardInterrupt) as error:
            try:
                for key, value in self.desired.items():
                    if self.command(("postconf", "-h", key)) not in (self.previous[key], value):
                        raise MigrationError("Target settings changed during shared-settings rollback")
                for entry in altered:
                    if optional_text(entry["file"]) not in (entry["before"], entry["text"]):
                        raise MigrationError("Target files changed during shared-settings rollback")
                    if entry["before"] is None:
                        for file in (entry["file"], *((Path(str(entry["file"]) + ".db"),) if entry["map"] else ())):
                            path = local_path(file)
                            if path.exists():
                                path.unlink()
                    else:
                        write_atomic_text(entry["file"], entry["before"], replace=True, private=bool(entry["map"]))
                        if entry["map"]:
                            self.command(("postmap", "hash:" + entry["map"]))
                for key in self.desired:
                    self.command(("postconf", "-e", key + " = " + self.previous[key]))
                self.command(("postfix", "check"))
                self.command(("postfix", "reload"))
                write_private_json(self.file, {**self.journal, "pending": False, "backup": str(backup), "rolled_back": True}, replace=True)
            except (MigrationError, OSError) as rollback:
                raise MigrationError("Shared Postfix update and rollback failed; retain {}. Update: {}; rollback: {}".format(
                    self.file, error, rollback,
                )) from rollback
            if isinstance(error, KeyboardInterrupt):
                raise
            raise MigrationError("Shared Postfix update failed and was rolled back; retain {}. {}".format(backup, error)) from error
