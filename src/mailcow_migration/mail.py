import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from .accounts import account_journal, account_mutations, require_before_activation, validate_account_journal, verify_account_inventory
from .configuration import Configuration
from .core.commands import run_command
from .core.errors import ConfigurationError, MigrationError
from .core.rsync import append_maildir
from .core.state import Workspace, require_identity
from .core.storage import local_path, private_directory, read_json, write_private_json
from .plans.recovery import recovery_plan
from .plans.saved import SavedPlan, load_saved_plan, validate_plan_inventory
from .plans.transfer import MAILDIR_CHECK, SENDER_OPTIONS, assert_append_compatible, receiver_options, rsync_capabilities, target_maildir
from .sources import SourceKind
from .sources.delivery import SourceDelivery
from .sources.inventory import inspect_source
from .sources.logins import SourceLogins
from .sources.maildirs import mailbox_path, source_identity
from .sources.roundcube import roundcube_sessions
from .targets.account_inventory import account_inventory
from .targets.isolation import TargetIsolation
from .targets.mailcow import MailcowTarget
from .targets.readiness import inspect_target_host
from .targets.storage import inspect_mail_storage


@dataclass(frozen=True)
class MailTransfer:
    batch: SavedPlan
    configuration: Configuration

    @property
    def file(self) -> Path:
        return local_path(self.batch.directory / "transfers.json")

    @property
    def identity(self) -> Mapping[str, Any]:
        return {"version": 1, "kind": "mailcow-transfer", "hash": self.batch.fingerprint, "source": asdict(self.batch.source),
                "api": self.configuration.mailcow.api, "mailcow_directory": self.configuration.mailcow.directory}

    def state(self) -> Mapping[str, Any]:
        if not self.file.exists():
            return {**self.identity, "mailPending": False, "finalMail": False, "sieve": False, "sievePending": False}
        state = read_json(self.file, private=True)
        if (not isinstance(state, dict) or type(state.get("version")) is not int
                or any(type(state.get(key)) is not bool for key in ("mailPending", "finalMail", "sieve", "sievePending"))
                or state["mailPending"] and state["finalMail"]):
            raise MigrationError("Invalid transfer journal; retain " + str(self.file))
        require_identity(state, self.identity)
        return state

    def save(self, state: Mapping[str, Any], **changes: Any) -> Mapping[str, Any]:
        following = {**state, **changes}
        write_private_json(self.file, following, replace=True)
        return following

    def check_isolation(self, target: MailcowTarget) -> None:
        state = account_journal(self.batch, self.configuration).read()
        require_before_activation(state)
        if state.get("accounts") is not True:
            raise MigrationError("Complete account staging for this plan before copying mail")
        recovery = recovery_plan(self.batch.plan)["emails"]
        validate_account_journal(state, account_mutations(self.batch.plan, recovery, reuse_domain=bool(state.get("existingDomain"))))
        verify_account_inventory(self.batch.plan, account_inventory(target.api), recovery, state)
        isolation = TargetIsolation(target, self.batch)
        paused = isolation.state()
        if paused.get("paused") is not True or paused.get("released") or paused.get("pending") or paused.get("routePending"):
            raise MigrationError("Complete target-isolation pause before copying mail; retain and resume any pending operation")
        isolation.check()
        if self.batch.plan["mailboxes"]:
            SourceLogins(self.batch).check()
        SourceDelivery(self.batch, self.configuration.mailcow.api).check()
        roundcube_sessions(self.batch, self.configuration).check()

    def run(self, *, apply: bool, final: bool) -> None:
        if self.batch.source.kind is not SourceKind.POSTFIXADMIN:
            raise MigrationError("Mail copying requires implemented source isolation; the Plesk isolation adapter is still pending")
        require_before_activation(account_journal(self.batch, self.configuration).read())
        state = self.state()
        if apply:
            state = self.save(state, mailPending=True, finalMail=False, mailStage="preflight", mailbox=None)
            print("Mail transfer journal: {}. This attempt invalidates any earlier final pass.".format(self.file))
        directory = local_path(self.batch.directory / "mail-transfer")
        try:
            if self.batch.blocking_findings:
                raise MigrationError("Resolve saved planning findings before copying mail:\n" + self.batch.blocking_findings)
            target = MailcowTarget(self.configuration.mailcow)
            self.check_isolation(target)
            inspect_target_host(target)
            storage = inspect_mail_storage(target)
            source = inspect_source(self.batch.source, self.batch.plan["domain"])
            validate_plan_inventory(self.batch.plan, source.inventory)
            identity = source_identity(source)
            measured = self.batch.plan.get("sourceUsage", {}).get("source")
            if measured is not None and (not isinstance(measured, dict) or any(
                    measured.get(key) != identity.get(key) for key in ("host", "root", "subdir"))):
                raise MigrationError("Mail source differs from the source used to calculate quotas; retain and reconcile the saved plan")
            binding = {"mailSource": identity, "containerRoot": str(storage.root), "uid": storage.uid, "gid": storage.gid}
            if any(key in state and state[key] != value for key, value in binding.items()):
                raise MigrationError("Source, mail volume or vmail ownership changed since transfer started; retain the journal")
            if apply:
                state = self.save(state, **binding)
            private_directory(directory)
            options = receiver_options(storage)
            connection = self.batch.source.ssh
            if connection.configuration_file and not os.access(connection.configuration_file, os.R_OK):
                raise MigrationError("SSH configuration file is not readable")
            ssh_options = connection.rsync_arguments()
            if self.batch.plan["mailboxes"]:
                rsync_capabilities(lambda args, label: run_command(
                    ("rsync", *args), label="Receiver rsync " + label, environment={"LC_ALL": "C"},
                    diagnostic_file=directory / ("receiver-rsync-" + uuid4().hex + ".txt"),
                ), (*options, "--checksum", "--dry-run", "--stats", "--out-format=", "--ignore-existing", "--info=progress2"))
                rsync_capabilities(lambda args, label: connection.run(
                    ("env", "LC_ALL=C", "rsync", *args), diagnostic_file=directory / ("source-rsync-" + uuid4().hex + ".txt"),
                ), SENDER_OPTIONS)
            copies = tuple((row["username"], mailbox_path(source, self.batch.plan["domain"], row["payload"]["local_part"]),
                            target_maildir(storage, self.batch.plan["domain"], row["payload"]["local_part"]))
                           for row in self.batch.plan["mailboxes"])
            for username, path, destination in copies:
                connection.run(
                    ("sh", "-c", MAILDIR_CHECK, "maildir-check", str(path), *(str(path / name) for name in ("cur", "new", "tmp"))),
                    diagnostic_file=directory / ("source-maildir-" + uuid4().hex + ".txt"),
                )
                print(username + ": source Maildir checked.")
            for index, (username, path, destination) in enumerate(copies, 1):
                print("Mailbox {}/{}: {}; checking append compatibility.".format(index, len(copies), username), flush=True)
                local_path(destination)
                remote = identity["host"] + ":" + str(path) + "/"
                assert_append_compatible(remote, destination, options, ssh_options, directory,
                                         directory / ("append-check-" + uuid4().hex + ".txt"))
                if apply:
                    state = self.save(state, mailStage="copy mail", mailbox=username)
                    local_path(destination)
                    run_command(
                        ("install", "-d", "-o", str(storage.uid), "-g", str(storage.gid),
                         str(destination.parent.parent), str(destination.parent), str(destination)),
                        label="Create owned Maildir directories", diagnostic_file=directory / ("maildir-create-" + uuid4().hex + ".txt"),
                    )
                if not apply and not destination.parent.exists():
                    print("Would create {} and append its source Maildir.".format(destination))
                else:
                    local_path(destination)
                    append_maildir(remote, destination, options, ssh_options, directory, preview=not apply)
                print(username + (": append completed." if apply else ": append preview completed."))
            if apply:
                state = self.save(state, mailStage="verify isolation", mailbox=None)
                self.check_isolation(target)
                current = inspect_source(self.batch.source, self.batch.plan["domain"])
                validate_plan_inventory(self.batch.plan, current.inventory)
                if source_identity(current) != identity:
                    raise MigrationError("Mail source changed during copying; the final pass was not recorded")
                current_storage = inspect_mail_storage(target)
                if (current_storage.root, current_storage.uid, current_storage.gid) != (storage.root, storage.uid, storage.gid):
                    raise MigrationError("Target mail volume or vmail ownership changed during copying; the final pass was not recorded")
                self.save(state, mailPending=False, finalMail=final, mailStage="complete")
                print("Mail append completed; final pass recorded: {}.".format("yes" if final else "no"))
            else:
                print("Mail preview completed; target mail and the transfer journal were not changed.")
        except (MigrationError, OSError, KeyboardInterrupt):
            if apply:
                print("Transfer stopped. Retain {} and {}; inspect the saved stage before retrying.".format(self.file, directory))
            raise


def run_mail(configuration: Configuration, file: Path, *, apply: bool = False, final: bool = False, writers_stopped: bool = False) -> int:
    if sys.platform != "linux" or os.geteuid() != 0:
        raise ConfigurationError("Run mail transfer on the Linux Mailcow host as root")
    if not writers_stopped:
        raise ConfigurationError("Use --writers-stopped only after stopping batch writers, submission and account administration on both servers")
    with Workspace().lock():
        MailTransfer(load_saved_plan(file, configuration), configuration).run(apply=apply, final=final)
    print("Keep source and target isolation in place. Sieve, activation, integrated webmail import and handoff remain pending.")
    return 0
