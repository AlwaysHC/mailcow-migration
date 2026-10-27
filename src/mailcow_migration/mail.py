import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .configuration import Configuration
from .core.commands import run_command
from .core.errors import MigrationError
from .core.rsync import append_maildir
from .core.state import Workspace
from .core.storage import local_path, private_directory
from .plans.saved import load_saved_plan, validate_plan_inventory
from .plans.transfer import (
    MAILDIR_CHECK,
    SENDER_OPTIONS,
    assert_append_compatible,
    receiver_options,
    rsync_capabilities,
    target_maildir,
)
from .sources.inventory import inspect_source
from .sources.maildirs import mailbox_path, source_identity
from .targets.mailcow import MailcowTarget
from .targets.readiness import inspect_target_host
from .targets.storage import inspect_mail_storage
from .transfers import TransferContext, require_transfer_host


@dataclass(frozen=True)
class MailTransfer(TransferContext):
    def run(self, *, apply: bool, final: bool) -> None:
        self.before_transfer()
        state = self.state()
        if apply:
            state = self.save(
                state,
                mailPending=True,
                finalMail=False,
                mailStage="preflight",
                mailbox=None,
            )
            print(
                f"Mail transfer journal: {self.file}. This attempt invalidates any earlier final pass."
            )
        directory = local_path(self.batch.directory / "mail-transfer")
        try:
            target = MailcowTarget(self.configuration.mailcow)
            self.check_isolation(target)
            inspect_target_host(target)
            storage = inspect_mail_storage(target)
            source = inspect_source(self.batch.source, self.batch.plan["domain"])
            validate_plan_inventory(self.batch.plan, source.inventory)
            identity = source_identity(source)
            measured = self.batch.plan.get("sourceUsage", {}).get("source")
            if measured is not None and (
                not isinstance(measured, dict)
                or any(
                    measured.get(key) != identity.get(key)
                    for key in ("host", "root", "subdir")
                )
            ):
                raise MigrationError(
                    "Mail source differs from the source used to calculate quotas; retain and reconcile the saved plan"
                )
            binding = {"mailSource": identity, **self.storage_binding(state, storage)}
            if "mailSource" in state and state["mailSource"] != identity:
                raise MigrationError(
                    "Mail source changed since transfer started; retain the journal"
                )
            if apply:
                state = self.save(state, **binding)
            private_directory(directory)
            options = receiver_options(storage)
            connection = self.batch.source.ssh
            if connection.configuration_file and not os.access(
                connection.configuration_file, os.R_OK
            ):
                raise MigrationError("SSH configuration file is not readable")
            ssh_options = connection.rsync_arguments()
            if self.batch.plan["mailboxes"]:
                rsync_capabilities(
                    lambda args, label: run_command(
                        ("rsync", *args),
                        label="Receiver rsync " + label,
                        environment={"LC_ALL": "C"},
                        diagnostic_file=directory
                        / ("receiver-rsync-" + uuid4().hex + ".txt"),
                    ),
                    (
                        *options,
                        "--checksum",
                        "--dry-run",
                        "--stats",
                        "--out-format=",
                        "--ignore-existing",
                        "--info=progress2",
                    ),
                )
                rsync_capabilities(
                    lambda args, label: connection.run(
                        ("env", "LC_ALL=C", "rsync", *args),
                        diagnostic_file=directory
                        / ("source-rsync-" + uuid4().hex + ".txt"),
                    ),
                    SENDER_OPTIONS,
                )
            copies = tuple(
                (
                    row["username"],
                    mailbox_path(
                        source, self.batch.plan["domain"], row["payload"]["local_part"]
                    ),
                    target_maildir(
                        storage, self.batch.plan["domain"], row["payload"]["local_part"]
                    ),
                )
                for row in self.batch.plan["mailboxes"]
            )
            for username, path, destination in copies:
                connection.run(
                    (
                        "sh",
                        "-c",
                        MAILDIR_CHECK,
                        "maildir-check",
                        str(path),
                        *(str(path / name) for name in ("cur", "new", "tmp")),
                    ),
                    diagnostic_file=directory
                    / ("source-maildir-" + uuid4().hex + ".txt"),
                )
                print(username + ": source Maildir checked.")
            for index, (username, path, destination) in enumerate(copies, 1):
                print(
                    f"Mailbox {index}/{len(copies)}: {username}; checking append compatibility.",
                    flush=True,
                )
                local_path(destination)
                remote = identity["host"] + ":" + str(path) + "/"
                assert_append_compatible(
                    remote,
                    destination,
                    options,
                    ssh_options,
                    directory,
                    directory / ("append-check-" + uuid4().hex + ".txt"),
                )
                if apply:
                    state = self.save(state, mailStage="copy mail", mailbox=username)
                    local_path(destination)
                    run_command(
                        (
                            "install",
                            "-d",
                            "-o",
                            str(storage.uid),
                            "-g",
                            str(storage.gid),
                            str(destination.parent.parent),
                            str(destination.parent),
                            str(destination),
                        ),
                        label="Create owned Maildir directories",
                        diagnostic_file=directory
                        / ("maildir-create-" + uuid4().hex + ".txt"),
                    )
                if not apply and not destination.parent.exists():
                    print(f"Would create {destination} and append its source Maildir.")
                else:
                    local_path(destination)
                    append_maildir(
                        remote,
                        destination,
                        options,
                        ssh_options,
                        directory,
                        preview=not apply,
                    )
                print(
                    username
                    + (
                        ": append completed."
                        if apply
                        else ": append preview completed."
                    )
                )
            if apply:
                state = self.save(state, mailStage="verify isolation", mailbox=None)
                self.check_isolation(target)
                current = inspect_source(self.batch.source, self.batch.plan["domain"])
                validate_plan_inventory(self.batch.plan, current.inventory)
                if source_identity(current) != identity:
                    raise MigrationError(
                        "Mail source changed during copying; the final pass was not recorded"
                    )
                current_storage = inspect_mail_storage(target)
                if (current_storage.root, current_storage.uid, current_storage.gid) != (
                    storage.root,
                    storage.uid,
                    storage.gid,
                ):
                    raise MigrationError(
                        "Target mail volume or vmail ownership changed during copying; the final pass was not recorded"
                    )
                self.save(
                    state, mailPending=False, finalMail=final, mailStage="complete"
                )
                print(
                    "Mail append completed; final pass recorded: {}.".format(
                        "yes" if final else "no"
                    )
                )
            else:
                print(
                    "Mail preview completed; target mail and the transfer journal were not changed."
                )
        except (MigrationError, OSError, KeyboardInterrupt):
            if apply:
                print(
                    f"Transfer stopped. Retain {self.file} and {directory}; inspect the saved stage before retrying."
                )
            raise


def run_mail(
    configuration: Configuration,
    file: Path,
    *,
    apply: bool = False,
    final: bool = False,
    writers_stopped: bool = False,
) -> int:
    require_transfer_host(writers_stopped)
    with Workspace().lock():
        MailTransfer(load_saved_plan(file, configuration), configuration).run(
            apply=apply, final=final
        )
    print(
        "Keep both servers isolated until Sieve, webmail, activation and handoff checks are complete."
    )
    return 0
