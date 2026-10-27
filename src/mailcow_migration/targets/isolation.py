import json
import re
import ssl
import time
from dataclasses import dataclass
from http.client import HTTPException
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPSHandler, build_opener
from uuid import uuid4

from ..core.commands import CommandResult
from ..core.errors import MigrationError
from ..core.state import require_identity
from ..core.storage import (
    decode_json,
    json_fingerprint,
    local_path,
    optional_text,
    read_json,
    write_atomic_text,
    write_private_json,
)
from ..plans.saved import SavedPlan
from ..resources import read_resource
from .mailcow import MailcowTarget, NoRedirect
from .quota_state import QuotaJournal
from .storage import inspect_container, mail_volume_root
from .webmail_state import WebmailJournal
from .writers import check_mailbox_writers

ACCESS = (
    "imap_access",
    "pop3_access",
    "smtp_access",
    "sieve_access",
    "sogo_access",
    "eas_access",
    "dav_access",
)
HOLD = "error:4.3.2 Domain migration in progress"
BEGIN, END = "# BEGIN migration delivery holds", "# END migration delivery holds"
DELIVERY_BLOCK = re.compile(re.escape(BEGIN) + r"\n[\s\S]*?" + re.escape(END) + r"\n?")
HOLD_LOOKUP = re.compile(r"regexp:/opt/postfix/conf/migration-hold-[a-f0-9]+")


def valid_switch(value: Any) -> bool:
    return type(value) in (int, str) and value in (0, 1, "0", "1")


@dataclass(frozen=True)
class TargetIsolation:
    target: MailcowTarget
    batch: SavedPlan
    webmail_repair: bool = False

    @property
    def file(self) -> Path:
        return local_path(
            self.batch.directory
            / (
                "webmail-target-isolation.json"
                if self.webmail_repair
                else "target-isolation.json"
            )
        )

    @property
    def identity(self) -> Mapping[str, Any]:
        return {
            "version": 1,
            "kind": "mailcow-target-isolation",
            "plan": self.batch.fingerprint,
            "api": self.target.settings.api,
            "mailcow_directory": self.target.settings.directory,
            **({"purpose": "webmail-repair"} if self.webmail_repair else {}),
        }

    def previous_isolation(self) -> Optional[Mapping[str, Any]]:
        if not self.webmail_repair:
            return None
        previous = TargetIsolation(self.target, self.batch).state()
        if (
            previous.get("released") is not True
            or previous.get("pending")
            or previous.get("routePending")
        ):
            raise MigrationError(
                "Webmail repair requires completed target access restoration for the same plan; retain the initial journal"
            )
        return previous

    @property
    def usernames(self) -> Tuple[str, ...]:
        return tuple(row["username"] for row in self.batch.plan["mailboxes"])

    @property
    def tag(self) -> str:
        return json_fingerprint(self.batch.plan["domain"])[:16]

    @property
    def lookup(self) -> str:
        return "regexp:/opt/postfix/conf/migration-hold-" + self.tag

    @property
    def extra_file(self) -> Path:
        return Path(self.target.settings.directory) / "data/conf/postfix/extra.cf"

    @property
    def guards(self) -> Mapping[Path, str]:
        directory = Path(self.target.settings.directory) / "data/conf"
        domains = "|".join(re.escape(domain) for domain in self.batch.plan["domains"])
        return {
            directory / "postfix" / ("migration-hold-" + self.tag): "/^([^@]+@)?("
            + domains
            + ")$/ "
            + HOLD
            + "\n",
            directory
            / "nginx"
            / ("site.migration-" + self.tag + ".custom"): "# Migration "
            + self.batch.plan["domain"]
            + "\n"
            'if ($uri ~* "^/(?:SOGo/)?(?:so|dav)/[^/]*@(?:'
            + domains
            + ')(?:/|$)") { return 503; }\n',
        }

    def state(self) -> Mapping[str, Any]:
        self.previous_isolation()
        state = (
            read_json(self.file, private=True)
            if self.file.exists()
            else {"identity": self.identity, "users": {}, "jobs": {}, "files": {}}
        )
        if not isinstance(state, dict) or not isinstance(state.get("identity"), dict):
            raise MigrationError(f"Invalid target isolation journal: {self.file}")
        require_identity(state["identity"], self.identity)
        if any(
            not isinstance(state.get(key), dict) for key in ("users", "jobs", "files")
        ) or state.get("pending") not in (None, "pause", "release"):
            raise MigrationError(
                "Invalid target isolation state; retain its recovery journal"
            )
        for user, attributes in state["users"].items():
            if (
                user not in self.usernames
                or not isinstance(attributes, dict)
                or set(attributes) != set(ACCESS)
                or any(
                    type(attributes[key]) is not int or attributes[key] not in (0, 1)
                    for key in ACCESS
                )
            ):
                raise MigrationError("Invalid saved target protocol settings")
        for job in state["jobs"].values():
            if (
                not isinstance(job, dict)
                or job.get("user2") not in self.usernames
                or job.get("active") not in (0, 1)
            ):
                raise MigrationError("Invalid saved target sync job")
        for name, change in state["files"].items():
            if (
                name not in {str(path) for path in self.guards}
                or not isinstance(change, dict)
                or "before" not in change
                or not isinstance(change.get("after"), str)
                or not (change["before"] is None or isinstance(change["before"], str))
            ):
                raise MigrationError("Invalid target configuration backup")
        pending = state.get("routePending")
        if pending is not None and (
            not isinstance(pending, dict)
            or any(
                not isinstance(pending.get(key), str)
                for key in ("before", "after", "chain", "desired")
            )
        ):
            raise MigrationError("Invalid pending delivery-route update")
        return state

    def update(self, **changes: Any) -> None:
        write_private_json(self.file, {**self.state(), **changes}, replace=True)

    def command(
        self,
        service: str,
        arguments: Sequence[str],
        *,
        input_text: Optional[str] = None,
        allowed_codes: Sequence[int] = (0,),
    ) -> CommandResult:
        return self.target.compose(
            ("exec", "-T", service, *arguments),
            input_text=input_text,
            allowed_codes=allowed_codes,
            diagnostic_file=self.batch.directory
            / ("target-isolation-" + uuid4().hex + ".txt"),
        )

    def postfix(self, arguments: Sequence[str]) -> str:
        return self.command("postfix-mailcow", arguments).stdout.strip()

    def inventory(self) -> Tuple[list, list]:
        users = [
            row
            for row in self.target.api.inventory("mailbox")
            if row.get("username") in self.usernames
        ]
        if len(users) != len(self.usernames) or len(
            {row["username"] for row in users}
        ) != len(self.usernames):
            raise MigrationError("Stage all batch mailboxes before isolating Mailcow")
        for user in users:
            attributes = (
                user.get("attributes")
                if isinstance(user.get("attributes"), dict)
                else {}
            )
            for name in ACCESS:
                if not valid_switch(attributes.get(name)):
                    raise MigrationError(
                        "{}: missing or invalid protocol control {}".format(
                            user["username"], name
                        )
                    )
        jobs = [
            row
            for row in self.target.api.inventory("syncjobs")
            if row.get("user2") in self.usernames
        ]
        if any(
            type(job.get("id")) not in (str, int)
            or str(job["id"]) == ""
            or not valid_switch(job.get("active"))
            for job in jobs
        ):
            raise MigrationError(
                "Mailcow returned a batch sync job without a valid ID/activity flag; cannot save its original state"
            )
        if len({str(job["id"]) for job in jobs}) != len(jobs):
            raise MigrationError("Mailcow returned duplicate sync-job identities")
        return users, jobs

    def running_jobs(self, *, stop: bool = False) -> list:
        result = self.command(
            "dovecot-mailcow",
            ("python3", "-c", read_resource("target_syncjobs.py")),
            input_text=json.dumps(
                {"users": self.usernames, "stop": stop}, allow_nan=False
            ),
        )
        try:
            jobs = decode_json(result.stdout)
        except (ValueError, UnicodeError) as error:
            raise MigrationError(
                "Invalid running sync-job inventory; contents omitted"
            ) from error
        if not isinstance(jobs, list) or any(
            not isinstance(row, dict)
            or row.get("user") not in self.usernames
            or type(row.get("pid")) is not int
            or row["pid"] <= 0
            for row in jobs
        ):
            raise MigrationError(
                "Invalid running sync-job identities; contents omitted"
            )
        return jobs

    def workers(self, service: str, names: str) -> Tuple[str, ...]:
        output = self.command(
            service,
            ("sh", "-s", "--", names),
            input_text=read_resource("container-workers.sh"),
        ).stdout
        values = tuple(output.split())
        if any(not re.fullmatch(r"[0-9]+", value) for value in values):
            raise MigrationError("Invalid container worker inventory")
        return values

    def drain(self, service: str, names: str, previous: Sequence[str]) -> None:
        for attempt in range(40):
            remaining = tuple(
                pid for pid in self.workers(service, names) if pid in previous
            )
            if not remaining:
                return
            if attempt == 39:
                raise MigrationError(
                    "{}: old workers have not drained; rerun. PIDs: {}".format(
                        service, ", ".join(remaining)
                    )
                )
            time.sleep(0.25)

    def check_file(self, name: Path, text: str) -> Optional[str]:
        current, known = optional_text(name), self.state()["files"].get(str(name))
        if known and current not in (known["before"], known["after"]):
            raise MigrationError(f"Configuration changed outside migration: {name}")
        if not known and current is not None and current != text:
            previous = self.previous_isolation()
            owned = (
                (previous["files"].get(str(name)) or {}).get("after")
                if previous is not None
                else None
            )
            if current != owned:
                raise MigrationError(f"Unowned configuration: {name}")
        return current

    def record_file(self, name: Path, text: str) -> None:
        current = self.check_file(name, text)
        files = self.state()["files"]
        self.update(
            files={
                **files,
                str(name): {
                    "before": files[str(name)]["before"]
                    if str(name) in files
                    else current,
                    "after": text,
                },
            }
        )
        if current != text:
            write_atomic_text(name, text, replace=True)

    def route_change(self, paused: bool) -> Mapping[str, str]:
        chain = self.postfix(("postconf", "-h", "transport_maps"))
        maps = tuple(item for item in re.split(r"[\s,]+", chain) if item)
        if self.lookup in maps and any(
            not HOLD_LOOKUP.fullmatch(item) for item in maps[: maps.index(self.lookup)]
        ):
            raise MigrationError(
                "Target migration hold has conflicting transport-map precedence"
            )
        desired = ", ".join(
            (
                *((self.lookup,) if paused else ()),
                *(item for item in maps if item != self.lookup),
            )
        )
        extra = optional_text(self.extra_file) or ""
        blocks = tuple(DELIVERY_BLOCK.finditer(extra))
        if len(blocks) > 1 or (BEGIN in extra or END in extra) and not blocks:
            raise MigrationError(f"Malformed managed delivery block: {self.extra_file}")
        if blocks:
            recorded = re.search(
                r"^transport_maps\s*=\s*(.*)$", blocks[0][0], re.MULTILINE
            )
            if not recorded or recorded[1] != chain:
                raise MigrationError(
                    "Effective target transport_maps differs from the persistent migration block"
                )
        following = (
            DELIVERY_BLOCK.sub("", extra).rstrip()
            + "\n"
            + BEGIN
            + "\ntransport_maps = "
            + desired
            + "\n"
            + END
            + "\n"
        )
        return {"before": extra, "after": following, "chain": chain, "desired": desired}

    def activate_route(self, change: Mapping[str, str]) -> None:
        write_atomic_text(self.extra_file, change["after"], replace=True)
        self.postfix(("postconf", "-e", "transport_maps = " + change["desired"]))
        self.postfix(("postfix", "check"))
        self.postfix(("postfix", "reload"))
        if self.postfix(("postconf", "-h", "transport_maps")) != change["desired"]:
            raise MigrationError("Target transport map activation failed")

    def pending_route(self) -> Optional[Mapping[str, str]]:
        pending = self.state().get("routePending")
        if pending and (
            (optional_text(self.extra_file) or "")
            not in (pending["before"], pending["after"])
            or self.postfix(("postconf", "-h", "transport_maps"))
            not in (pending["chain"], pending["desired"])
        ):
            raise MigrationError(
                f"Interrupted target delivery update conflicts with live configuration. Journal: {self.file}"
            )
        return pending

    def change_route(self, paused: bool) -> None:
        pending = self.pending_route()
        if pending:
            self.activate_route(pending)
            self.update(routePending=None)
        change = self.route_change(paused)
        if paused:
            for name, text in self.guards.items():
                if name.parent.name == "postfix":
                    self.record_file(name, text)
        backup = self.batch.directory / (
            "target-postfix-" + json_fingerprint(change["before"]) + ".json"
        )
        write_private_json(
            backup, {"extra": change["before"], "chain": change["chain"]}, replace=True
        )
        self.update(routePending=change)
        self.activate_route(change)
        if paused:
            for username in self.usernames:
                if self.postfix(("postmap", "-q", username, self.lookup)) != HOLD:
                    raise MigrationError("Target delivery is not held: " + username)
        self.update(routePending=None)

    def webmail_supported(self) -> None:
        effective = self.command("nginx-mailcow", ("nginx", "-T")).stdout
        if "include /etc/nginx/conf.d/site.*.custom;" not in effective:
            raise MigrationError(
                "Mailcow nginx does not load site.*.custom; cannot install the domain webmail maintenance guard"
            )

    def guard_webmail(self, paused: bool) -> None:
        self.webmail_supported()
        for name, text in self.guards.items():
            if name.parent.name == "nginx":
                self.record_file(
                    name,
                    text
                    if paused
                    else "# Migration "
                    + self.batch.plan["domain"]
                    + ": access released\n",
                )
        self.command("nginx-mailcow", ("nginx", "-t"))
        self.command("nginx-mailcow", ("nginx", "-s", "reload"))

    def preflight(self, *, require_accounts: bool = True) -> Tuple[list, list]:
        state = self.state()
        if state.get("released") or state.get("pending") == "release":
            raise MigrationError(
                "Target access restoration has started; do not copy into live mailboxes"
            )
        if re.search(
            r"/transport_maps\s*=", self.postfix(("postconf", "-P")), re.MULTILINE
        ):
            raise MigrationError(
                "A Mailcow Postfix service overrides transport_maps; isolation adapter required"
            )
        users, jobs = self.inventory() if require_accounts else ([], [])
        self.running_jobs()
        _, sogo = inspect_container(self.target, "sogo-mailcow")
        network = sogo.get("NetworkSettings")
        ports = network.get("Ports") if isinstance(network, dict) else None
        if (
            not isinstance(network, dict)
            or ports is not None
            and not isinstance(ports, dict)
        ):
            raise MigrationError("Cannot inspect SOGo published ports")
        if any(
            not isinstance(bindings, list)
            or any(
                not isinstance(binding, dict)
                or binding.get("HostIp") not in ("127.0.0.1", "::1")
                for binding in bindings
            )
            for bindings in (ports or {}).values()
            if bindings is not None
        ):
            raise MigrationError(
                "SOGo has an unknown or published port bypassing nginx; domain webmail isolation cannot be established"
            )
        if re.search(
            r"^\s*mail_replica\s*=\s*\S",
            self.command("dovecot-mailcow", ("doveconf", "-n")).stdout,
            re.MULTILINE,
        ):
            raise MigrationError(
                "Mailcow Dovecot replication is configured; its per-domain writer needs a pause adapter"
            )
        self.webmail_supported()
        for name, text in self.guards.items():
            self.check_file(name, text)
        if not self.pending_route():
            self.route_change(True)
        return users, jobs

    def check_webmail(self, username: str) -> None:
        opener = build_opener(
            NoRedirect(), HTTPSHandler(context=ssl.create_default_context())
        )
        url = self.target.settings.api + "/SOGo/so/" + quote(username, safe="") + "/"
        try:
            with opener.open(url, timeout=15) as response:
                status = response.status
        except HTTPError as error:
            status = error.code
            error.close()
        except (URLError, OSError, HTTPException) as error:
            raise MigrationError(
                f"Cannot verify SOGo maintenance guard for {username} ({type(error).__name__})"
            ) from error
        if status != 503:
            raise MigrationError(
                f"SOGo maintenance guard failed for {username}: HTTP {status}"
            )

    def check(self) -> None:
        users, jobs = self.inventory()
        for user in users:
            if any(int(user["attributes"][name]) != 0 for name in ACCESS):
                raise MigrationError(
                    user["username"] + ": Mailcow access is no longer paused"
                )
            if self.command(
                "dovecot-mailcow",
                ("doveadm", "-f", "flow", "who", "-1", user["username"]),
            ).stdout.strip():
                raise MigrationError("Mailcow sessions remain for " + user["username"])
        if any(int(job["active"]) for job in jobs):
            raise MigrationError("A batch Mailcow sync job is active again")
        if self.running_jobs():
            raise MigrationError(
                "Mailcow sync jobs have not stopped; rerun pause to resume. No copy started"
            )
        for name, text in self.guards.items():
            if optional_text(name) != text:
                raise MigrationError(f"Mailcow isolation guard changed: {name}")
        for username in self.usernames:
            self.check_webmail(username)
        chain = tuple(
            item
            for item in re.split(
                r"[\s,]+", self.postfix(("postconf", "-h", "transport_maps"))
            )
            if item
        )
        if self.lookup not in chain or any(
            not HOLD_LOOKUP.fullmatch(item)
            for item in chain[: chain.index(self.lookup)]
        ):
            raise MigrationError("Mailcow delivery hold changed")
        _, container = inspect_container(self.target, "dovecot-mailcow")
        check_mailbox_writers(
            mail_volume_root(container), self.usernames, self.batch.plan["domains"]
        )

    def pause(self) -> None:
        users, jobs = self.preflight()
        state = self.state()
        original_users = {
            row["username"]: {name: int(row["attributes"][name]) for name in ACCESS}
            for row in users
        }
        original_jobs = {
            str(row["id"]): {"active": int(row["active"]), "user2": row["user2"]}
            for row in jobs
        }
        self.update(
            users={**original_users, **state["users"]},
            jobs={**original_jobs, **state["jobs"]},
            pending="pause",
        )
        print(
            f"Mailcow pause/release journal: {self.file}. Rerun the same pause command to resume."
        )
        delivery_names = "qmgr lmtp local virtual pipe"
        old_delivery = self.workers("postfix-mailcow", delivery_names)
        self.change_route(True)
        self.drain("postfix-mailcow", delivery_names, old_delivery)
        print("Domain delivery deferred; old delivery workers drained.")
        master = self.command(
            "nginx-mailcow", ("cat", "/var/run/nginx.pid")
        ).stdout.strip()
        if not re.fullmatch(r"[0-9]+", master):
            raise MigrationError("Cannot identify the nginx master process")
        old_web = tuple(
            pid for pid in self.workers("nginx-mailcow", "nginx") if pid != master
        )
        self.guard_webmail(True)
        self.drain("nginx-mailcow", "nginx", old_web)
        print("Domain SOGo resource paths blocked; old web workers drained.")
        for job in jobs:
            if int(job["active"]):
                self.target.api.request(
                    "edit/syncjob", {"items": [str(job["id"])], "attr": {"active": 0}}
                )
        for row in self.running_jobs(stop=True):
            print(
                "Stopping batch imapsync worker: {} (PID {})".format(
                    row["user"], row["pid"]
                )
            )
        for user in users:
            username = user["username"]
            if any(int(user["attributes"][name]) for name in ACCESS):
                self.target.api.request(
                    "edit/mailbox",
                    {"items": [username], "attr": dict.fromkeys(ACCESS, 0)},
                )
            self.command(
                "dovecot-mailcow", ("doveadm", "auth", "cache", "flush", username)
            )
            result = self.command(
                "dovecot-mailcow", ("doveadm", "kick", username), allowed_codes=(0, 68)
            )
            if result.returncode == 68 and (
                result.stdout.strip() != "no users kicked" or result.stderr.strip()
            ):
                raise MigrationError(
                    "Unexpected Dovecot disconnect result for "
                    + username
                    + "; isolation was not marked complete"
                )
            print(username + ": protocol access paused and Dovecot disconnected.")
        self.check()
        self.update(pending=None, paused=True)

    def release(self, account_state: Mapping[str, Any]) -> None:
        if not self.file.exists():
            return
        state = self.state()
        if not account_state.get("activated") or account_state.get("pending"):
            raise MigrationError(
                "Finish activation and webmail import before releasing Mailcow"
            )
        WebmailJournal(self.batch, self.target.settings).require_complete()
        QuotaJournal(self.batch, self.target.settings).require_complete(self.target)
        if state.get("released"):
            return
        self.update(pending="release")
        print(
            f"Restoring saved Mailcow access and delivery. Recovery journal: {self.file}"
        )
        users, jobs = self.inventory()
        for user in users:
            previous = state["users"].get(user["username"])
            if not previous or any(
                int(user["attributes"][name]) not in (0, previous[name])
                for name in ACCESS
            ):
                raise MigrationError(
                    "Mailcow access changed outside migration: " + user["username"]
                )
            if any(int(user["attributes"][name]) != previous[name] for name in ACCESS):
                self.target.api.request(
                    "edit/mailbox", {"items": [user["username"]], "attr": previous}
                )
            self.command(
                "dovecot-mailcow",
                ("doveadm", "auth", "cache", "flush", user["username"]),
            )
        self.guard_webmail(False)
        self.change_route(False)
        for identifier, original in state["jobs"].items():
            job = next((row for row in jobs if str(row["id"]) == identifier), None)
            if not job or job["user2"] != original["user2"]:
                raise MigrationError(
                    "Paused sync job changed or disappeared: " + identifier
                )
            if int(job["active"]) != original["active"]:
                self.target.api.request(
                    "edit/syncjob",
                    {"items": [identifier], "attr": {"active": original["active"]}},
                )
        actual_users, actual_jobs = self.inventory()
        for user in actual_users:
            if any(
                int(user["attributes"][name]) != state["users"][user["username"]][name]
                for name in ACCESS
            ):
                raise MigrationError(
                    "Restored Mailcow access did not verify: " + user["username"]
                )
        for identifier, original in state["jobs"].items():
            found = next(
                (row for row in actual_jobs if str(row["id"]) == identifier), None
            )
            if not found or int(found["active"]) != original["active"]:
                raise MigrationError(
                    "Restored Mailcow sync job did not verify: " + identifier
                )
        self.update(pending=None, released=True)
        print(
            "Mailcow access, delivery and saved sync-job states restored; source logins remain unchanged."
        )


def active_target_isolation(
    target: MailcowTarget, batch: SavedPlan, *, webmail_repair: bool = False
) -> TargetIsolation:
    repair = TargetIsolation(target, batch, webmail_repair=True)
    return (
        repair
        if webmail_repair or repair.file.exists()
        else TargetIsolation(target, batch)
    )
