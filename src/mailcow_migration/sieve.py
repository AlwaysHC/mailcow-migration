import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from .configuration import Configuration
from .core.commands import run_command
from .core.errors import MigrationError
from .core.state import Workspace
from .core.storage import local_path, private_directory, read_json
from .plans.evidence import load_export
from .plans.native_settings import mailbox_sieve
from .plans.saved import load_saved_plan, validate_plan_inventory
from .plans.sieve_evidence import load_sieve_evidence, sieve_inventory_blockers
from .targets.mailcow import MailcowTarget
from .targets.sieve import SieveScript, read_sieve_file, sieve_scripts
from .targets.storage import inspect_mail_storage
from .transfers import TransferContext, require_transfer_host


@dataclass(frozen=True)
class SieveTransfer(TransferContext):
    def backup(self, state: Mapping[str, Any], script: SieveScript, directory: Path) -> Mapping[str, Any]:
        backup = local_path(Path(tempfile.mkdtemp(prefix="sieve-backup-", dir=str(self.batch.directory))))
        state = self.save(state, sieveStage="back up target Sieve", sieveUser=script.username,
                          sieveBackups=[*state.get("sieveBackups", []), str(backup)])
        for path, content in ((script.target, script.previous), (script.binary, script.compiled)):
            if content is not None:
                run_command(("install", "-m", "600", str(path), str(backup / path.name)), label="Back up target Sieve",
                            diagnostic_file=directory / ("sieve-backup-" + uuid4().hex + ".txt"))
                if read_sieve_file(backup / path.name, backup) != content:
                    raise MigrationError(script.username + ": Sieve backup differs from preflight; no replacement attempted")
        print("Private Sieve backup for {}: {}".format(script.username, backup))
        return state

    def run(self, exported: Path, *, apply: bool, use_default: bool, replace: bool) -> None:
        self.before_transfer()
        state = self.state()
        if apply:
            state = self.save(state, sieve=False, sievePending=True, sieveStage="preflight", sieveUser=None)
            print("Sieve transfer journal: {}. This attempt invalidates any earlier Sieve completion.".format(self.file))
        directory = local_path(self.batch.directory / "sieve-transfer")
        try:
            evidence = load_export(self.configuration, self.batch.plan["domain"], exported)
            manifest = read_json(self.batch.directory / "manifest.json", private=True)
            if (not isinstance(manifest, dict) or evidence.manifest["export_id"] != manifest.get("export_id")
                    or evidence.source.connection != self.batch.source):
                raise MigrationError("Use the completed export selected by this saved plan; retain its original evidence")
            validate_plan_inventory(self.batch.plan, evidence.source.inventory)
            source = load_sieve_evidence(self.batch.plan, evidence)
            blockers = sieve_inventory_blockers(source)
            for row in self.batch.plan["mailboxes"]:
                try:
                    mailbox_sieve(row["username"], source)
                except MigrationError as error:
                    blockers.append(row["username"] + ": " + str(error))
            if blockers:
                raise MigrationError("Sieve migration blocked:\n" + "\n".join(blockers))
            target = MailcowTarget(self.configuration.mailcow)
            self.check_isolation(target)
            storage = inspect_mail_storage(target)
            binding = self.storage_binding(state, storage)
            root = local_path(storage.root / "sieve")
            effective = target.compose(("exec", "-T", "dovecot-mailcow", "doveconf", "-h", "plugin/sieve")).stdout.strip()
            if effective != "/var/vmail/sieve/%u.sieve":
                raise MigrationError("Target Sieve path differs from the supported layout")
            scripts, fingerprint = sieve_scripts(self.batch.plan, evidence, storage, use_default=use_default, replace=replace)
            evidence.verify()
            if apply:
                state = self.save(state, **binding, sieveExport=str(evidence.directory),
                                  sieveEvidenceHash=fingerprint, defaultSieve=use_default)
            private_directory(directory)
            for index, script in enumerate(scripts, 1):
                print("Sieve mailbox {}/{}: {}".format(index, len(scripts), script.username), flush=True)
                if not apply:
                    if script.conflict:
                        print("Would back up conflicting script/binary under {}.".format(self.batch.directory))
                    print("Would install and compile Sieve." if script.content is not None
                          else "Would remove personal Sieve and its stale binary, if present, and use target default filtering.")
                    continue
                script.check_unchanged(evidence.data, root)
                if script.conflict:
                    state = self.backup(state, script, directory)
                script.check_unchanged(evidence.data, root)
                state = self.save(state, sieveStage="install and compile" if script.content is not None else "select default filtering",
                                  sieveUser=script.username)
                if script.content is not None:
                    run_command(("install", "-m", "600", "-o", str(storage.uid), "-g", str(storage.gid), str(script.source), str(script.target)),
                                label="Install personal Sieve", diagnostic_file=directory / ("sieve-install-" + uuid4().hex + ".txt"))
                    target.compose(("exec", "-T", "--user", "vmail", "dovecot-mailcow", "sievec",
                                    "/var/vmail/sieve/" + script.username + ".sieve"),
                                   diagnostic_file=directory / ("sieve-compile-" + uuid4().hex + ".txt"))
                    if read_sieve_file(script.target, root) != script.content or read_sieve_file(script.binary, root) is None:
                        raise MigrationError(script.username + ": installed/compiled Sieve readback failed; retain the journal")
                else:
                    for path, content in ((script.target, script.previous), (script.binary, script.compiled)):
                        if content is not None:
                            local_path(path).unlink()
                    if read_sieve_file(script.target, root) is not None or read_sieve_file(script.binary, root) is not None:
                        raise MigrationError(script.username + ": personal Sieve removal did not complete; retain the journal")
                    print(script.username + ": target default filtering selected.")
            evidence.verify()
            if apply:
                state = self.save(state, sieveStage="verify isolation", sieveUser=None)
                self.check_isolation(target)
                self.storage_binding(state, inspect_mail_storage(target))
                self.save(state, sieve=True, sievePending=False, sieveStage="complete")
                print("Sieve installation and compilation completed. Keep all isolation in place.")
            else:
                print("Sieve preview completed; no target scripts were written, compiled or removed, and transfer receipts were unchanged.")
        except (MigrationError, OSError, KeyboardInterrupt):
            if apply:
                print("Sieve transfer stopped. Retain {} and all recorded backups; inspect the saved stage before retrying.".format(self.file))
            raise


def run_sieve(
    configuration: Configuration, file: Path, exported: Path, *, apply: bool = False, use_default: bool = False,
    replace: bool = False, writers_stopped: bool = False,
) -> int:
    require_transfer_host(writers_stopped)
    with Workspace().lock():
        SieveTransfer(load_saved_plan(file, configuration), configuration).run(exported, apply=apply, use_default=use_default, replace=replace)
    print("SOGo filter/settings integration, quota recalculation, activation and handoff remain separate requirements.")
    return 0
