import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple
from uuid import uuid4

from . import SourceKind
from ..core.commands import CommandResult
from ..core.errors import MigrationError
from ..core.state import require_identity
from ..core.storage import decode_json, local_path, read_json, write_private_json
from ..plans.saved import SavedPlan
from ..resources import php_resource

BASE_QUERY = "SELECT username AS user,password FROM mailbox WHERE username = '%u' AND active='1'"
EXCLUSION = re.compile(r" AND username NOT IN \('(?:[^'\\\r\n]|'')*'(?:, '(?:[^'\\\r\n]|'')*')*\)")
SQL_VALUE = re.compile(r"'((?:[^']|'')*)'")
BACKUPS = "/root/mailcow-in-progress/auth-backups/"


def query_members(clause: str) -> Tuple[str, ...]:
    return tuple(sorted(match[1].replace("''", "'") for match in SQL_VALUE.finditer(clause)))


def login_query(query: str, users: Tuple[str, ...], *, freeze: bool) -> Tuple[str, bool]:
    clauses = query[len(BASE_QUERY):]
    if not query.startswith(BASE_QUERY) or EXCLUSION.sub("", clauses):
        raise MigrationError("The live password_query is not the supported source query with optional migration exclusions. "
                             "No changes made; review only its password_query line without sharing database credentials")
    existing = tuple(match[0] for match in EXCLUSION.finditer(clauses))
    own = tuple(clause for clause in existing if query_members(clause) == users)
    if any(clause not in own and set(query_members(clause)).intersection(users) for clause in existing):
        raise MigrationError("Another login exclusion overlaps this batch; resolve its ownership before changing source authentication")
    if not freeze:
        return BASE_QUERY + "".join(clause for clause in existing if clause not in own), bool(own)
    exclusion = " AND username NOT IN (" + ", ".join("'" + user.replace("'", "''") + "'" for user in users) + ")"
    return query if own else query + exclusion, bool(own)


def validate_passdb(effective: str) -> None:
    blocks = tuple(re.finditer(r"^passdb\s*\{([^{}]*)^\}", effective, re.M))
    if (len(blocks) != 1 or len(re.findall(r"^\s*passdb\s*\{", effective, re.M)) != 1
            or not re.search(r"^\s*driver\s*=\s*sql\s*$", blocks[0][1], re.M)
            or not re.search(r"^\s*args\s*=\s*/etc/dovecot/dovecot-mysql\.conf\s*$", blocks[0][1], re.M)
            or any(line.strip() and not re.match(r"\s*(?:args|driver)\s*=", line) for line in blocks[0][1].splitlines())):
        raise MigrationError("Automatic login isolation requires the single SQL passdb in /etc/dovecot/dovecot-mysql.conf. "
                             "The live authentication chain differs; no source configuration was changed")


@dataclass(frozen=True)
class LoginSnapshot:
    query: str = field(repr=False)
    configuration_hash: str
    dovecot_hash: str


@dataclass(frozen=True)
class SourceLogins:
    batch: SavedPlan

    def __post_init__(self) -> None:
        if self.batch.source.kind is not SourceKind.POSTFIXADMIN:
            raise MigrationError("This login step supports PostfixAdmin; Plesk requires its separate login and delivery isolation adapter")
        if len(set(self.users)) != len(self.users) or any(not re.fullmatch(r"[a-zA-Z0-9_.+%-]+@[a-zA-Z0-9.-]+", user) for user in self.users):
            raise MigrationError("Source login isolation requires literal, non-wildcard mailbox names; unsupported name in this batch")

    @property
    def users(self) -> Tuple[str, ...]:
        return tuple(sorted(row["username"] for row in self.batch.plan["mailboxes"]))

    @property
    def file(self) -> Path:
        return local_path(self.batch.directory / "source-logins.json")

    @property
    def identity(self) -> Mapping[str, Any]:
        return {"version": 1, "kind": "postfixadmin-login-isolation", "plan": self.batch.fingerprint,
                "source": asdict(self.batch.source), "users": list(self.users)}

    def record(self) -> Optional[Mapping[str, Any]]:
        if not self.file.exists():
            return None
        record = read_json(self.file, private=True)
        if not isinstance(record, dict):
            raise MigrationError("Invalid source login journal")
        require_identity(record, self.identity)
        if (record.get("action") not in ("freeze", "release") or type(record.get("complete")) is not bool
                or any(not isinstance(record.get(key), str) for key in ("queryBefore", "queryAfter", "stage"))
                or any(not isinstance(record.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", record[key])
                       for key in ("configuration_hash", "dovecot_hash"))
                or record.get("backup") is not None and (not isinstance(record["backup"], str)
                                                        or not re.fullmatch(re.escape(BACKUPS) + r"[a-f0-9]{64}\.conf", record["backup"]))):
            raise MigrationError("Invalid source login recovery record; retain it before further changes")
        return record

    def command(self, arguments: Sequence[str], *, input_text: Optional[str] = None, allowed_codes: Sequence[int] = (0,)) -> CommandResult:
        return self.batch.source.ssh.run(arguments, input_text=input_text, allowed_codes=allowed_codes,
                                         diagnostic_file=self.batch.directory / ("source-logins-" + uuid4().hex + ".txt"))

    def configuration(self, action: str, **values: str) -> Mapping[str, str]:
        result = self.command(("php", "-d", "display_errors=stderr", "-r", php_resource("postfixadmin-logins.php")),
                              input_text=json.dumps({"action": action, **values}, allow_nan=False))
        try:
            response = decode_json(result.stdout)
        except (ValueError, UnicodeError) as error:
            raise MigrationError("Invalid source login configuration response; contents omitted") from error
        if (not isinstance(response, dict) or not isinstance(response.get("hash"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", response["hash"])
                or action == "read" and not isinstance(response.get("query"), str)
                or action == "write" and response.get("backup") != BACKUPS + values["expectedHash"] + ".conf"):
            raise MigrationError("Incomplete source login configuration response; retain the journal before retrying")
        return response

    def effective_configuration(self) -> str:
        return self.command(("doveconf", "-n")).stdout.strip()

    def inspect(self) -> LoginSnapshot:
        self.record()
        self.command(("sh", "-c", 'for tool in php cp doveconf doveadm; do command -v "$tool" >/dev/null || '
                      '{ printf "Required source command is missing: %s\\n" "$tool" >&2; exit 1; }; done'))
        effective = self.effective_configuration()
        validate_passdb(effective)
        current = self.configuration("read")
        login_query(current["query"], self.users, freeze=True)
        return LoginSnapshot(current["query"], current["hash"], hashlib.sha256(effective.encode("utf-8")).hexdigest())

    def check_configuration(self, snapshot: LoginSnapshot) -> None:
        current = self.configuration("read")
        effective = self.effective_configuration()
        if (current["query"] != snapshot.query or current["hash"] != snapshot.configuration_hash
                or hashlib.sha256(effective.encode("utf-8")).hexdigest() != snapshot.dovecot_hash):
            raise MigrationError("Source authentication configuration changed; stop and rerun preview to review it again")

    def check_user(self, user: str, *, disconnect: bool = False, checkpoint: Callable[[str], None] = lambda stage: None) -> None:
        checkpoint("verify authentication exclusion for " + user)
        lookup = self.command(("doveadm", "auth", "lookup", "-f", "user", user), allowed_codes=(0, 67))
        if lookup.returncode != 67:
            raise MigrationError("Dovecot still returns a password-database entry for {}; new authentication is not isolated".format(user))
        if disconnect:
            checkpoint("disconnect Dovecot sessions for " + user)
            result = self.command(("doveadm", "kick", user), allowed_codes=(0, 68))
            if result.returncode == 68 and (result.stdout.strip() != "no users kicked" or result.stderr.strip()):
                raise MigrationError("Unexpected Dovecot disconnect result for " + user + "; login isolation was not marked complete")
        checkpoint("verify no Dovecot sessions remain for " + user)
        for attempt in range(10):
            if not self.command(("doveadm", "-f", "flow", "who", "-1", user)).stdout.strip():
                return
            if attempt < 9:
                time.sleep(0.25)
        raise MigrationError("Dovecot sessions remain for {}. No forced process termination attempted; "
                             "a shared process could contain other domains' users".format(user))

    def check(self) -> None:
        record = self.record()
        if not record or record["action"] != "freeze" or not record["complete"]:
            raise MigrationError("No completed source login freeze to check; resume the saved operation first")
        snapshot = LoginSnapshot(record["queryAfter"], record["configuration_hash"], record["dovecot_hash"])
        self.check_configuration(snapshot)
        for user in self.users:
            self.check_user(user)

    def apply(self, snapshot: LoginSnapshot, *, freeze: bool) -> None:
        replacement, _ = login_query(snapshot.query, self.users, freeze=freeze)
        previous = self.record()
        backup = BACKUPS + snapshot.configuration_hash + ".conf" if replacement != snapshot.query else previous.get("backup") if previous else None
        record = {**self.identity, "action": "freeze" if freeze else "release", "queryBefore": snapshot.query,
                  "queryAfter": replacement, "backup": backup, "complete": False,
                  "configuration_hash": snapshot.configuration_hash, "dovecot_hash": snapshot.dovecot_hash}
        stage = "prepare login query"

        def checkpoint(following: str) -> None:
            nonlocal stage
            stage = following
            write_private_json(self.file, {**record, "stage": stage}, replace=True)

        checkpoint(stage)
        print("Source login operation journal: {}".format(self.file))
        try:
            if replacement != snapshot.query:
                checkpoint("back up and update login query")
                result = self.configuration("write", expectedHash=snapshot.configuration_hash, query=replacement)
                record = {**record, "configuration_hash": result["hash"]}
            checkpoint("verify login query")
            self.check_configuration(LoginSnapshot(replacement, record["configuration_hash"], snapshot.dovecot_hash))
            checkpoint("reload Dovecot")
            self.command(("doveadm", "reload"))
            for user in self.users:
                checkpoint("clear authentication cache for " + user)
                self.command(("doveadm", "auth", "cache", "flush", user))
                if freeze:
                    self.check_user(user, disconnect=True, checkpoint=checkpoint)
                print(user + (": authentication blocked; no Dovecot sessions remain." if freeze else ": authentication cache cleared."))
            write_private_json(self.file, {**record, "stage": "complete", "complete": True}, replace=True)
        except (MigrationError, OSError) as error:
            raise MigrationError("{}\nStopped while attempting to {}. The login query may already be installed. "
                                 "Retain {} and the backups on source {}: {}. Rerun to retry freeze or release before activation; "
                                 "do not restore a whole configuration over later batches.".format(
                                     error, stage, self.file, self.batch.source.ssh.host, backup or "no file edit was needed")) from error
