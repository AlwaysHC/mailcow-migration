import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from .accounts import (
    account_activation_mutations, account_journal, account_mutations, require_before_activation, validate_account_journal, verify_account_inventory,
)
from .configuration import Configuration
from .core.errors import ConfigurationError, MigrationError
from .core.state import require_identity
from .core.storage import local_path, read_json, write_private_json
from .plans.recovery import recovery_plan
from .plans.saved import SavedPlan
from .sources import SourceKind
from .sources.delivery import SourceDelivery
from .sources.logins import SourceLogins
from .sources.roundcube import roundcube_sessions
from .targets.account_inventory import account_inventory
from .targets.isolation import TargetIsolation
from .targets.mailcow import MailcowTarget
from .targets.storage import MailStorage


def require_mailcow_host() -> None:
    if sys.platform != "linux" or os.geteuid() != 0:
        raise ConfigurationError("Run this migration operation on the Linux Mailcow host as root")


def require_transfer_host(writers_stopped: bool) -> None:
    require_mailcow_host()
    if not writers_stopped:
        raise ConfigurationError("Use --writers-stopped only after stopping batch writers, submission and account administration on both servers")


@dataclass(frozen=True)
class TransferContext:
    batch: SavedPlan
    configuration: Configuration

    @property
    def file(self) -> Path:
        return local_path(self.batch.directory / "transfers.json")

    @property
    def identity(self) -> Mapping[str, Any]:
        return {"version": 1, "kind": "mailcow-transfer", "hash": self.batch.fingerprint, "source": asdict(self.batch.source),
                "api": self.configuration.mailcow.api, "mailcow_directory": self.configuration.mailcow.directory}

    def before_transfer(self) -> None:
        if self.batch.source.kind is not SourceKind.POSTFIXADMIN:
            raise MigrationError("Transfer requires implemented source isolation; the Plesk isolation adapter is still pending")
        require_before_activation(account_journal(self.batch, self.configuration).read())

    def state(self) -> Mapping[str, Any]:
        if not self.file.exists():
            return {**self.identity, "mailPending": False, "finalMail": False, "sieve": False, "sievePending": False}
        state = read_json(self.file, private=True)
        if (not isinstance(state, dict) or type(state.get("version")) is not int
                or any(type(state.get(key)) is not bool for key in ("mailPending", "finalMail", "sieve", "sievePending"))
                or state["mailPending"] and state["finalMail"] or state["sievePending"] and state["sieve"]):
            raise MigrationError("Invalid transfer journal; retain " + str(self.file))
        require_identity(state, self.identity)
        if "sieveBackups" in state and (not isinstance(state["sieveBackups"], list)
                                       or any(not isinstance(path, str) for path in state["sieveBackups"])):
            raise MigrationError("Invalid Sieve backup inventory; retain " + str(self.file))
        return state

    def save(self, state: Mapping[str, Any], **changes: Any) -> Mapping[str, Any]:
        following = {**state, **changes}
        write_private_json(self.file, following, replace=True)
        return following

    def final_state(self) -> Mapping[str, Any]:
        state = self.state()
        if state["mailPending"] or not state["finalMail"] or state["sievePending"] or not state["sieve"]:
            raise MigrationError("Complete the final mail and Sieve passes before activation or webmail import")
        return state

    def storage_binding(self, state: Mapping[str, Any], storage: MailStorage) -> Mapping[str, Any]:
        binding = {"containerRoot": str(storage.root), "uid": storage.uid, "gid": storage.gid}
        if any(key in state and state[key] != value for key, value in binding.items()):
            raise MigrationError("Mail volume or vmail ownership changed since transfer started; retain the journal")
        return binding

    def check_accounts(self, target: MailcowTarget, *, activated: bool = False) -> Mapping[str, list]:
        state = account_journal(self.batch, self.configuration).read()
        if activated:
            if state.get("activated") is not True or state.get("pending"):
                raise MigrationError("Complete account activation before webmail import; reconcile any pending request")
        else:
            require_before_activation(state)
        if state.get("accounts") is not True:
            raise MigrationError("Complete account staging for this plan before transfer")
        recovery = recovery_plan(self.batch.plan)["emails"]
        inventory = account_inventory(target.api)
        operations = account_mutations(self.batch.plan, recovery, reuse_domain=bool(state.get("existingDomain")))
        activation = account_activation_mutations(self.batch.plan, inventory) if activated else ()
        validate_account_journal(state, operations, activation)
        verify_account_inventory(self.batch.plan, inventory, recovery, state)
        return inventory

    def check_isolation(self, target: MailcowTarget) -> None:
        findings = self.batch.blocking_findings
        if findings:
            raise MigrationError("Resolve saved planning findings before transfer:\n" + findings)
        self.check_accounts(target)
        self.check_holds(target)

    def target_holds(self, target: MailcowTarget) -> TargetIsolation:
        isolation = TargetIsolation(target, self.batch)
        paused = isolation.state()
        if paused.get("paused") is not True or paused.get("released") or paused.get("pending") or paused.get("routePending"):
            raise MigrationError("Complete target-isolation pause before migration work; retain and resume any pending operation")
        return isolation

    def check_holds(self, target: MailcowTarget) -> None:
        self.target_holds(target).check()
        if self.batch.plan["mailboxes"]:
            SourceLogins(self.batch).check()
        SourceDelivery(self.batch, self.configuration.mailcow.api).check()
        roundcube_sessions(self.batch, self.configuration).check()
