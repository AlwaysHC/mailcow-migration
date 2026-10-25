import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional
from uuid import uuid4

from .php import source_php_file
from ..configuration import Configuration, MailcowConfiguration, absolute_path
from ..core.errors import MigrationError
from ..core.folders import folder_inventory
from ..core.state import require_identity
from ..core.storage import json_fingerprint, local_path, read_json, read_text, write_private_json, write_private_text
from ..plans.roundcube import roundcube_plan, roundcube_summary
from ..plans.saved import SavedPlan


def session_directory(batch: SavedPlan) -> Path:
    return local_path(batch.directory / "roundcube-sessions")


def roundcube_sessions(batch: SavedPlan, configuration: Configuration, directory: Optional[str] = None) -> "RoundcubeSessions":
    file = local_path(session_directory(batch) / "state.json")
    previous = read_json(file, private=True) if file.exists() else None
    if previous is not None and not isinstance(previous, dict):
        raise MigrationError("Invalid RoundCube session journal")
    selected = directory or (previous.get("directory") if previous else None) or batch.source.roundcube_directory
    if selected is None:
        raise MigrationError("Specify the RoundCube installation on the source with roundcube-sessions --directory DIR; "
                             "it must contain config/config.inc.php")
    sessions = RoundcubeSessions(batch, absolute_path(selected), configuration.mailcow)
    sessions.record()
    return sessions


@dataclass(frozen=True)
class RoundcubeSessions:
    batch: SavedPlan
    directory: str
    target: MailcowConfiguration

    @property
    def file(self) -> Path:
        return local_path(session_directory(self.batch) / "state.json")

    @property
    def identity(self) -> Mapping[str, Any]:
        return {"version": 1, "kind": "roundcube-session-isolation", "plan": self.batch.fingerprint,
                "source": asdict(self.batch.source), "directory": self.directory, "api": self.target.api,
                "mailcow_directory": self.target.directory, "source_isolated": False, "import_complete": False}

    def counts(self, value: Any) -> Mapping[str, int]:
        if not isinstance(value, dict):
            raise MigrationError("Invalid RoundCube session counts; contents omitted")
        targets = {row["username"] for row in self.batch.plan["mailboxes"]}
        for login, count in value.items():
            parts = login.split("@")
            if (len(parts) != 2 or parts[1] not in self.batch.plan["domains"]
                    or parts[0] + "@" + self.batch.plan["domain"] not in targets or type(count) is not int or count < 0):
                raise MigrationError("RoundCube session ownership/counts differ from the saved batch; contents omitted")
        return value

    def record(self) -> Optional[Mapping[str, Any]]:
        if not self.file.exists():
            return None
        record = read_json(self.file, private=True)
        if not isinstance(record, dict):
            raise MigrationError("Invalid RoundCube session journal")
        require_identity(record, self.identity)
        fingerprint = record.get("source_sha256")
        if (not isinstance(fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", fingerprint)
                or record.get("snapshot") != "inventory-" + fingerprint + ".json"
                or type(record.get("complete")) is not bool or type(record.get("drafts_saved")) is not bool
                or record.get("stage") not in ("preview", "revoke sessions", "verify sessions", "verify source", "complete")
                or not isinstance(record.get("blockers"), list) or any(not isinstance(value, str) for value in record["blockers"])
                or not isinstance(record.get("attempts"), list)):
            raise MigrationError("Invalid RoundCube session recovery record; retain its snapshots before further changes")
        self.counts(record.get("preview_sessions"))
        for attempt in record["attempts"]:
            if (not isinstance(attempt, dict) or type(attempt.get("complete")) is not bool
                    or not isinstance(attempt.get("source_sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", attempt["source_sha256"])
                    or not isinstance(attempt.get("started_at"), str)):
                raise MigrationError("Invalid RoundCube revocation receipt")
            self.counts(attempt.get("before"))
            for key in ("removed", "after"):
                if attempt.get(key) is not None:
                    self.counts(attempt[key])
                elif attempt["complete"]:
                    raise MigrationError("Incomplete RoundCube revocation receipt marked complete")
            if attempt["complete"] and any(attempt["after"].values()):
                raise MigrationError("RoundCube revocation receipt still contains active sessions")
        if record["complete"] and (record["stage"] != "complete" or not record["drafts_saved"]
                                   or not record["attempts"] or not record["attempts"][-1]["complete"]):
            raise MigrationError("RoundCube session completion differs from its operation receipts")
        if "proposal_sha256" in record:
            proposal = record["proposal_sha256"]
            if (not isinstance(proposal, str) or not re.fullmatch(r"[a-f0-9]{64}", proposal)
                    or record.get("proposal") != "proposal-" + proposal + ".json"
                    or type(record.get("reviewed")) is not bool or type(record.get("deferrals_accepted")) is not bool):
                raise MigrationError("Invalid saved SOGo proposal review; retain the session journal")
        return record

    def request(self, action: str) -> Mapping[str, Any]:
        if action not in ("snapshot", "inspect-sessions", "revoke-sessions"):
            raise MigrationError("Unsupported RoundCube source operation")
        response = source_php_file(self.batch.source, session_directory(self.batch), "roundcube-export.php", {
            "directory": self.directory, "domain": self.batch.plan["domain"], "domains": self.batch.plan["domains"],
            "users": [row["username"] for row in self.batch.plan["mailboxes"]], "sourceKind": self.batch.source.kind.value, "action": action,
        })
        if action == "snapshot":
            self.validate_snapshot(response)
            return response
        if response.get("action") != action:
            raise MigrationError("RoundCube session helper returned a different operation; retain the journal")
        return self.counts(response.get("sessions"))

    def validate_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        lists = ("users", "ignoredUsers", "unknownTables", "sharedDictionary")
        if (type(snapshot.get("format")) is not int or snapshot["format"] != 4 or not isinstance(snapshot.get("version"), str)
                or not re.fullmatch(r"1\.6\.[0-9]+", snapshot["version"]) or any(not isinstance(snapshot.get(key), list) for key in lists)
                or not isinstance(snapshot.get("directory"), str) or not snapshot["directory"].startswith("/")
                or not isinstance(snapshot.get("database"), str) or not re.fullmatch(r"[a-zA-Z0-9_]+", snapshot["database"])
                or not (isinstance(snapshot.get("defaults"), dict) or snapshot.get("defaults") == [])
                or any(not isinstance(table, str) for table in snapshot["unknownTables"])):
            raise MigrationError("Unsupported or incomplete RoundCube inventory; contents omitted")
        targets = {row["username"] for row in self.batch.plan["mailboxes"]}
        found = set()
        for user in snapshot["users"]:
            if (not isinstance(user, dict) or not isinstance(user.get("target"), str) or user["target"] not in targets
                    or user["target"] in found or not isinstance(user.get("id"), str) or not isinstance(user.get("login"), str)
                    or "lastLogin" not in user or not isinstance(user.get("preferencesSerialized"), str)
                    or not (isinstance(user.get("preferences"), dict) or user.get("preferences") == [])
                    or any(not isinstance(user.get(key), list) for key in (
                        "contacts", "groups", "members", "identities", "responses", "dictionary", "searches", "collectedAddresses", "files",
                    ))):
                raise MigrationError("RoundCube selected profiles differ from the saved mailbox plan; contents omitted")
            found.add(user["target"])
        if any(not isinstance(user, dict) or not isinstance(user.get("id"), str) or not isinstance(user.get("login"), str)
               for user in snapshot["ignoredUsers"]):
            raise MigrationError("Invalid ignored RoundCube profile inventory")

    def save_document(self, document: Mapping[str, Any], prefix: str) -> str:
        fingerprint = json_fingerprint(document, canonical=True)
        file = local_path(session_directory(self.batch) / (prefix + "-" + fingerprint + ".json"))
        if file.exists():
            if read_json(file, private=True) != document:
                raise MigrationError("Saved RoundCube evidence differs from its content fingerprint; retain the files")
        else:
            write_private_json(file, document)
        return fingerprint

    def save_snapshot(self, snapshot: Mapping[str, Any]) -> str:
        return self.save_document(snapshot, "inventory")

    def inspect_folders(self, username: str) -> Mapping[str, Any]:
        return folder_inventory(lambda arguments: self.batch.source.ssh.run(
            arguments, diagnostic_file=session_directory(self.batch) / ("folder-inspection-" + uuid4().hex + ".txt"),
        ).stdout, username)

    def save_proposal(self, snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
        proposal = roundcube_plan(snapshot, self.batch.source.ssh.host, self.batch.plan, self.batch.fingerprint, self.inspect_folders)
        fingerprint = self.save_document(proposal, "proposal")
        summary = roundcube_summary(proposal)
        file = local_path(session_directory(self.batch) / ("proposal-" + fingerprint + ".txt"))
        if file.exists():
            if read_text(file, private=True) != summary:
                raise MigrationError("Saved SOGo review summary changed; retain it and review the matching JSON proposal")
        else:
            write_private_text(file, summary)
        return proposal

    def preview(self, *, drafts_saved: bool = False, reviewed: bool = False, accept_deferrals: bool = False) -> Mapping[str, Any]:
        previous = self.record()
        snapshot = self.request("snapshot")
        fingerprint = self.save_snapshot(snapshot)
        proposal = self.save_proposal(snapshot)
        proposal_hash = json_fingerprint(proposal, canonical=True)
        sessions = self.request("inspect-sessions")
        same = previous is not None and previous["source_sha256"] == fingerprint
        same_proposal = bool(same and previous.get("proposal_sha256") == proposal_hash)
        pending = previous is not None and not previous["complete"] and previous["stage"] != "preview"
        if pending and (not same or "proposal_sha256" in previous and not same_proposal):
            raise MigrationError("A RoundCube session operation is unfinished. Its baseline was retained; "
                                 "resume revocation or reconcile the saved snapshots before replacing that baseline")
        verified = bool(same and previous["complete"] and not any(sessions.values()))
        review_matches = same_proposal and not proposal["blockers"]
        state = {**self.identity, "source_sha256": fingerprint, "snapshot": "inventory-" + fingerprint + ".json",
                 "stage": previous["stage"] if pending else "complete" if verified else "preview", "complete": verified,
                 "preview_sessions": sessions, "blockers": proposal["blockers"],
                 "proposal": "proposal-" + proposal_hash + ".json", "proposal_sha256": proposal_hash,
                 "reviewed": bool(review_matches and (reviewed or previous.get("reviewed"))),
                 "deferrals_accepted": bool(review_matches and (accept_deferrals or previous.get("deferrals_accepted"))),
                 "drafts_saved": drafts_saved or bool(same and previous["drafts_saved"]),
                 "attempts": previous["attempts"] if previous else []}
        write_private_json(self.file, state, replace=True)
        print("Private RoundCube inventory: {}".format(session_directory(self.batch) / state["snapshot"]))
        print("{} selected profiles; {} ignored profiles; {} database sessions for batch logins.".format(
            len(snapshot["users"]), len(snapshot["ignoredUsers"]), sum(sessions.values())))
        for user in snapshot["ignoredUsers"]:
            if user.get("reason") == "missing-mailbox":
                print("RoundCube profile {} ({}) has no batch mailbox; its persistent data remains on the source.".format(user["id"], user["login"]))
        if state["blockers"]:
            print("Resolve these preservation blockers before source isolation:\n" + "\n".join(state["blockers"]))
        print("SOGo proposal: {}".format(session_directory(self.batch) / state["proposal"]))
        print("Review summary: {}".format(session_directory(self.batch) / ("proposal-" + proposal_hash + ".txt")))
        print("{} contacts and {} identities proposed across {} profiles.".format(
            sum(len(user["records"]) for user in proposal["users"]), sum(user["identities"] for user in proposal["users"]), len(proposal["users"])))
        if proposal["deferredPreferences"] and not state["deferrals_accepted"]:
            print("{} preferences require explicit deferral approval; original values and proposed fallbacks are in the review.".format(
                len(proposal["deferredPreferences"])))
        if reviewed and not same_proposal:
            raise MigrationError("A new SOGo proposal was saved. Review its JSON/text files before rerunning preview with --reviewed")
        return state

    def proposal(self, record: Mapping[str, Any], *, require_review: bool = True) -> Mapping[str, Any]:
        if "proposal_sha256" not in record:
            raise MigrationError("Run RoundCube preview again to create and review its SOGo proposal; existing session receipts are retained")
        proposal = read_json(session_directory(self.batch) / record["proposal"], private=True)
        if (not isinstance(proposal, dict) or type(proposal.get("version")) is not int or proposal["version"] != 1
                or proposal.get("kind") != "roundcube-sogo-proposal" or proposal.get("preservation_version") != 3
                or proposal.get("source_sha256") != record["source_sha256"] or proposal.get("account_plan_sha256") != self.batch.fingerprint
                or proposal.get("target_verified") is not False or proposal.get("import_complete") is not False
                or not isinstance(proposal.get("users"), list) or not isinstance(proposal.get("blockers"), list)
                or not isinstance(proposal.get("deferredPreferences"), list)
                or json_fingerprint(proposal, canonical=True) != record["proposal_sha256"]):
            raise MigrationError("Saved SOGo proposal changed or belongs to another snapshot/plan; retain the original evidence")
        if proposal["blockers"]:
            raise MigrationError("RoundCube preservation is blocked:\n" + "\n".join(proposal["blockers"]))
        if require_review and not record["reviewed"]:
            raise MigrationError("Review the saved SOGo proposal and rerun roundcube-sessions preview with --reviewed")
        if require_review and proposal["deferredPreferences"] and not record["deferrals_accepted"]:
            raise MigrationError("Review the preserved preference values/fallbacks and explicitly accept their deferral with --accept-deferrals")
        return proposal

    def approved_record(self) -> Mapping[str, Any]:
        record = self.record()
        if record is None:
            raise MigrationError("Run roundcube-sessions preview before source isolation")
        snapshot = read_json(session_directory(self.batch) / record["snapshot"], private=True)
        if not isinstance(snapshot, dict) or json_fingerprint(snapshot, canonical=True) != record["source_sha256"]:
            raise MigrationError("RoundCube preview snapshot changed; retain the journal and original evidence")
        self.validate_snapshot(snapshot)
        self.proposal(record)
        return record

    def check_source(self, record: Mapping[str, Any]) -> None:
        current = self.request("snapshot")
        fingerprint = self.save_snapshot(current)
        if fingerprint != record["source_sha256"]:
            raise MigrationError("RoundCube user data changed after preview; current and approved snapshots were retained. "
                                 "Freeze webmail edits and reconcile the snapshots before continuing")
        for user in self.proposal(record, require_review=False)["users"]:
            folders = user["folderState"]
            if folders is not None:
                current = self.inspect_folders(user["username"])
                if current != folders["source"]:
                    self.save_document({"username": user["username"], "source": current}, "folder-inventory")
                    raise MigrationError(user["username"] + ": source folder tree changed after preview; retain the folder evidence and reconcile")

    def preflight(self, *, require_drafts: bool = True) -> Mapping[str, int]:
        record = self.approved_record()
        if require_drafts and not record["drafts_saved"]:
            raise MigrationError("Confirm every migrating user's browser-only drafts were saved on the server: "
                                 "run roundcube-sessions preview with --drafts-saved")
        self.check_source(record)
        return self.request("inspect-sessions")

    def check_empty(self) -> Mapping[str, int]:
        sessions = self.request("inspect-sessions")
        active = ["{}: {}".format(user, count) for user, count in sessions.items() if count]
        if active:
            raise MigrationError("Source RoundCube sessions remain: " + ", ".join(active))
        return sessions

    def check(self) -> None:
        record = self.approved_record()
        if not record["complete"]:
            raise MigrationError("No completed RoundCube session revocation to verify; resume the saved operation")
        self.check_empty()
        self.check_source(record)

    def revoke(self, *, drafts_saved: bool = False) -> None:
        record = self.approved_record()
        if not (drafts_saved or record["drafts_saved"]):
            raise MigrationError("Save every migrating user's browser-only drafts on the server and confirm with --drafts-saved")
        self.check_source(record)
        before = self.request("inspect-sessions")
        attempt = {"started_at": datetime.now(timezone.utc).isoformat(), "source_sha256": record["source_sha256"],
                   "before": before, "removed": None, "after": None, "complete": False}
        state = {**record, "stage": "revoke sessions", "complete": False, "drafts_saved": True,
                 "attempts": [*record["attempts"], attempt]}

        def checkpoint(stage: str) -> None:
            write_private_json(self.file, {**state, "stage": stage, "attempts": [*record["attempts"], attempt]}, replace=True)

        checkpoint("revoke sessions")
        print("RoundCube session operation journal: {}".format(self.file))
        try:
            removed = self.request("revoke-sessions")
            attempt = {**attempt, "removed": removed}
            checkpoint("verify sessions")
            for user, count in removed.items():
                print("{}: {} source RoundCube sessions revoked.".format(user, count))
            attempt = {**attempt, "after": self.check_empty()}
            checkpoint("verify source")
            self.check_source(record)
            attempt = {**attempt, "complete": True}
            state = {**state, "complete": True}
            checkpoint("complete")
        except (MigrationError, OSError) as error:
            raise MigrationError("{}\nRoundCube sessions may already have been revoked. Retain {} and its snapshots. "
                                 "Resume after checking source isolation; changed persistent data needs reconciliation.".format(
                                     error, self.file)) from error
