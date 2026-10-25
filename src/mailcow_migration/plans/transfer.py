import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

from .assessment import check
from ..core.commands import CommandResult, run_command
from ..core.errors import MigrationError
from ..core.storage import local_path
from ..sources.inventory import SourceInspection
from ..sources.maildirs import mailbox_path, source_identity
from ..targets.mailcow import MailcowTarget
from ..targets.storage import MailStorage, inspect_mail_storage

SENDER_OPTIONS = ("-aH", "--safe-links", "--itemize-changes", "--checksum", "--dry-run", "--stats", "--out-format=")
EXCLUSIONS = ("--exclude=dovecot.index*", "--exclude=dovecot.list.index*", "--exclude=maildirsize", "--exclude=**/tmp/*")
MAILDIR_CHECK = (
    'for directory in "$@"; do\n'
    '  test -d "$directory" && test ! -L "$directory" || {\n'
    '    printf "Missing or symlinked source Maildir: %s\\n" "$directory" >&2\n'
    '    exit 1\n'
    '  }\n'
    'done\n'
)


def receiver_options(storage: MailStorage) -> Tuple[str, ...]:
    return ("-aH", "--safe-links", "--itemize-changes", "--chown={}:{}".format(storage.uid, storage.gid), *EXCLUSIONS)


def rsync_capabilities(
    execute: Callable[[Sequence[str], str], CommandResult], options: Sequence[str],
) -> Mapping[str, Any]:
    banner = execute(("--version",), "version").stdout
    version = re.search(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?", banner)
    if version is None:
        raise MigrationError("Cannot identify rsync version")
    execute((*options, "--help"), "options")
    return {"version": version[0], "options": list(options), "verified": True}


def target_maildir(storage: MailStorage, domain: str, local_part: str) -> Path:
    path = local_path(storage.root / domain / local_part / "Maildir")
    if storage.root not in path.parents:
        raise MigrationError("Target mailbox must stay below the mail volume")
    ancestor = path
    while not ancestor.exists():
        ancestor = ancestor.parent
    if not ancestor.is_dir():
        raise MigrationError("Target mailbox ancestor is not a directory: {}".format(ancestor))
    return path


def maildir_message_id(file: str) -> Optional[str]:
    match = re.search(r"(?:^|/)(?:cur|new)/([^/]+)$", file)
    return match[1].split(":2,", 1)[0] if match else None


def target_messages(directory: Path) -> Mapping[str, list]:
    messages, pending = {}, [directory]
    while pending:
        folder = pending.pop()
        with os.scandir(folder) as entries:
            for entry in entries:
                file = Path(entry.path)
                if entry.is_symlink():
                    raise MigrationError("Append blocked by target symlink: {}".format(file))
                if entry.is_dir(follow_symlinks=False):
                    pending.append(file)
                else:
                    identifier = maildir_message_id(file.relative_to(directory).as_posix())
                    if identifier:
                        messages.setdefault(identifier, []).append(str(file))
    return messages


def inspect_append_inventory(directory: Path, inventory: Path, messages: Mapping[str, list]) -> int:
    count = 0
    try:
        with inventory.open("r", encoding="utf-8", newline="") as stream:
            for raw in stream:
                line = raw.rstrip("\n")
                if not line:
                    continue
                match = re.fullmatch(r"APPEND\|([^\r\n]{11})\|([^\r\n]+)", line)
                if match is None:
                    raise MigrationError("Cannot interpret append inventory; contents omitted")
                item, relative = match[1], match[2]
                if re.search(r"\\#[0-7]{3}", relative) or PurePosixPath(relative).is_absolute() or ".." in relative.split("/"):
                    raise MigrationError("Append inventory contains an unsupported path; contents omitted")
                file = local_path(directory / relative)
                exists = file.exists()
                if item[1] not in ("f", "d") or exists and file.is_dir() != (item[1] == "d"):
                    raise MigrationError("Append cannot preserve the file type at {}; no files copied".format(file))
                if item[1] == "f":
                    if exists and (item[0] == ">" or item[2] == "c" or item[3] == "s"):
                        raise MigrationError("Source and target contents differ at {}. Existing files must be retained; reconcile this conflict. "
                                             "Dovecot UID/keyword metadata requires a mailbox-aware merge; "
                                             "do not delete it to bypass this check.".format(file))
                    identifier = maildir_message_id(relative)
                    alternatives = messages.get(identifier) if identifier else None
                    if not exists and alternatives:
                        raise MigrationError("Source message {} already has a different target path: {}. "
                                             "It may have moved folders or changed flags; reconcile it before appending.".format(
                                                 relative, ", ".join(alternatives)))
                count += 1
    except UnicodeError as error:
        raise MigrationError("Append inventory is not valid UTF-8; contents omitted") from error
    return count


def assert_append_compatible(
    source_path: str, destination: Path, options: Sequence[str], ssh_options: Sequence[str], directory: Path, diagnostics: Path,
) -> int:
    if not destination.exists():
        return 0
    messages = target_messages(destination)
    with tempfile.TemporaryDirectory(prefix="append-check-", dir=str(directory)) as temporary:
        inventory = Path(temporary) / "inventory"
        run_command(
            ("rsync", *options, "--dry-run", "--checksum", "--out-format=APPEND|%i|%n", *ssh_options,
             "--", source_path, str(destination) + "/"),
            timeout=86400, label="Read-only append compatibility inventory", stdout_file=inventory,
            diagnostic_file=diagnostics, environment={"LC_ALL": "C"},
        )
        return inspect_append_inventory(destination, inventory, messages)


def transfer_assessment(
    plan: Mapping[str, Any], source: SourceInspection, target: MailcowTarget, directory: Path, *, source_checks: bool,
) -> Mapping[str, Any]:
    storage = inspect_mail_storage(target)
    identity = source_identity(source)
    measured = plan.get("sourceUsage", {}).get("source")
    if measured is not None and (not isinstance(measured, dict) or any(measured.get(key) != identity.get(key) for key in ("host", "root", "subdir"))):
        raise MigrationError("Mail source differs from the source used to calculate quotas; prepare a new plan before account creation")
    options = receiver_options(storage)
    checks = [check("mail-storage", "verified", "Resolved /var/vmail and vmail identity {}:{}".format(storage.uid, storage.gid))]
    receiver = None
    if plan["mailboxes"]:
        receiver = rsync_capabilities(lambda args, label: run_command(
            ("rsync", *args), label="Receiver rsync " + label, environment={"LC_ALL": "C"},
            diagnostic_file=directory / ("receiver-rsync-" + label + ".txt"),
        ), (*options, "--checksum", "--dry-run", "--stats", "--out-format=", "--ignore-existing", "--info=progress2"))
        checks.append(check("receiver-rsync", "verified", "Receiver accepts the required append, ownership, checksum and progress options"))
    else:
        checks.append(check("receiver-rsync", "not-needed", "No mailboxes to transfer"))
    sender, ssh_options = None, ()
    if source_checks and plan["mailboxes"]:
        connection = source.connection.ssh
        if connection.configuration_file and not os.access(connection.configuration_file, os.R_OK):
            raise MigrationError("SSH configuration file is not readable")
        ssh_options = connection.rsync_arguments()
        sender = rsync_capabilities(lambda args, label: connection.run(
            ("env", "LC_ALL=C", "rsync", *args), diagnostic_file=directory / ("source-rsync-" + label + ".txt"),
        ), SENDER_OPTIONS)
        checks.append(check("source-rsync", "verified", "Source accepts the required sender options"))
    elif plan["mailboxes"]:
        checks.append(check("source-rsync", "pending", "Source transfer checks are deferred for fixed quotas or source mapping blockers"))
    else:
        checks.append(check("source-rsync", "not-needed", "No mailboxes to transfer"))
    mailboxes = []
    for index, row in enumerate(plan["mailboxes"]):
        username = row["username"]
        current: Dict[str, Any] = {"username": username, "status": "pending"}
        if plan["blockers"]:
            checks.append(check("transfer-mailbox:" + username, "pending", "Resolve source mapping blockers before preparing transfer paths"))
            mailboxes.append(current)
            continue
        print("Read-only transfer check {}/{}: {}...".format(index + 1, len(plan["mailboxes"]), username))
        try:
            path = mailbox_path(source, plan["domain"], row["payload"]["local_part"])
            destination = target_maildir(storage, plan["domain"], row["payload"]["local_part"])
            remote = identity["host"] + ":" + str(path) + "/"
            current.update({"source": remote, "destination": str(destination), "creation_required": not destination.exists()})
            if source_checks:
                source.connection.ssh.run(
                    ("sh", "-c", MAILDIR_CHECK, "maildir-check", str(path), *(str(path / name) for name in ("cur", "new", "tmp"))),
                    diagnostic_file=directory / "source-maildir-{}.txt".format(index + 1),
                )
                entries = assert_append_compatible(
                    remote, destination, options, ssh_options, directory, directory / "append-check-{}.txt".format(index + 1),
                )
                current.update({"status": "verified", "inventory_entries": entries,
                                "append_check": "not-needed" if current["creation_required"] else "verified"})
                checks.append(check("transfer-mailbox:" + username, "verified", "Source Maildir and target path checked; "
                                    + ("target directory must be created during staging" if current["creation_required"]
                                       else "current contents pass append compatibility checks")))
            else:
                checks.append(check("transfer-mailbox:" + username, "pending",
                                    "Target path checked; source Maildir and append inventory are not verified"))
        except MigrationError as error:
            current.update({"status": "blocked", "reason": str(error)})
            checks.append(check("transfer-mailbox:" + username, "blocked", str(error)))
        mailboxes.append(current)
    checks.append(check("transfer-execution", "pending", "Account staging, matching journals, stopped source/target writers and pre-activation state "
                        "must be verified before transfer. Repeat the preview under isolation; no files have been copied."))
    return {"version": 1, "kind": "mail-transfer-assessment", "checked_at": datetime.now(timezone.utc).isoformat(),
            "transfer_ready": False, "account_journal_verified": False, "isolation_verified": False, "capacity_assessed": False,
            "source_checks_requested": source_checks, "source": identity, "storage": storage.document(),
            "receiver": receiver, "sender": sender, "mailboxes": mailboxes, "checks": checks}
