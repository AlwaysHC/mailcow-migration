from pathlib import Path
from typing import Mapping

from .admins import verify_admins
from .configuration import Configuration
from .core.errors import MigrationError
from .core.state import Workspace
from .core.storage import json_fingerprint, local_path
from .plans.evidence import load_plan_export
from .plans.saved import load_saved_plan
from .plans.saved_webmail import load_saved_webmail
from .plans.server_state import require_server_settings
from .senders import verify_senders
from .sogo_target import sogo_preparation
from .sources import SourceKind
from .sources.roundcube import roundcube_sessions
from .targets.mailcow import MailcowTarget
from .targets.prefilter_activation import activate_prefilters
from .targets.quota_state import QuotaJournal
from .targets.sogo import SogoTarget
from .targets.sogo_filters import SogoFilterImport
from .targets.sogo_import import SogoImport
from .targets.sogo_settings import SogoSettingsImport
from .targets.webmail_state import WebmailJournal
from .transfers import TransferContext, require_transfer_host


def run_webmail(
    configuration: Configuration, file: Path, exported: Path, *, apply: bool = False, writers_stopped: bool = False,
) -> int:
    require_transfer_host(writers_stopped)
    with Workspace().lock():
        batch = load_saved_plan(file, configuration)
        journal = WebmailJournal(batch, configuration.mailcow)
        previous = journal.read()
        if previous is not None and previous["complete"]:
            completed = journal.require_complete()
            print("Webmail import already completed; subsequent target user edits are retained.")
            if completed["deferred_preferences"]:
                print("{} approved preference deferrals remain in the saved RoundCube proposal.".format(completed["deferred_preferences"]))
            return 0
        if batch.blocking_findings:
            raise MigrationError("Resolve saved planning findings before webmail import:\n" + batch.blocking_findings)
        context = TransferContext(batch, configuration)
        target = MailcowTarget(configuration.mailcow)
        evidence = load_plan_export(configuration, batch, exported)
        reviewed = load_saved_webmail(batch, evidence)
        if reviewed.blockers:
            raise MigrationError("Resolve saved webmail findings before import:\n" + "\n".join(reviewed.blockers))
        preparation = sogo_preparation(configuration, batch)
        sessions = roundcube_sessions(batch, configuration)
        record = sessions.approved_record()
        if json_fingerprint(preparation.proposal, canonical=True) != record["proposal_sha256"]:
            raise MigrationError("RoundCube approval changed while preparing the import; retain the original proposal")
        inputs = {"export_id": evidence.manifest["export_id"], "export_sha256": json_fingerprint(evidence.manifest, canonical=True),
                  "source_sha256": record["source_sha256"], "proposal_sha256": record["proposal_sha256"],
                  "webmail_sha256": json_fingerprint(reviewed.proposal, canonical=True)}
        journal.read(inputs)
        native = SogoImport(preparation)
        filters = (SogoFilterImport(SogoTarget(target, local_path(batch.directory / "sogo-filters")), batch, evidence)
                   if batch.source.kind is SourceKind.POSTFIXADMIN else None)
        settings = SogoSettingsImport(filters, configuration) if filters is not None else None
        native.state()
        if filters is not None and settings is not None:
            filters.state(filters.users())
            settings.state(reviewed)
        deferred = len(preparation.proposal["deferredPreferences"])

        def verify_ready() -> None:
            require_server_settings(batch, configuration)
            context.final_state()
            inventory = context.check_accounts(target, activated=True)
            QuotaJournal(batch, configuration.mailcow).require_complete(target)
            verify_senders(context, target, inventory)
            verify_admins(context, target)
            context.check_holds(target, webmail=True)

        def check_source() -> None:
            evidence.verify()
            current = sessions.approved_record()
            if any(current[key] != record[key] for key in ("source_sha256", "proposal_sha256")):
                raise MigrationError("RoundCube approval changed during webmail import; retain its original snapshot and proposal")
            if json_fingerprint(load_saved_webmail(batch, evidence).proposal, canonical=True) != inputs["webmail_sha256"]:
                raise MigrationError("Saved webmail proposal changed during import; completion was not recorded")
            sessions.check_source(record)

        verify_ready()
        check_source()
        activate_prefilters(context, evidence, reviewed, verify_ready=verify_ready, activate=False)
        if not apply:
            if not native.state()["native_complete"]:
                native.inspect()
                print("RoundCube preference/contact/folder destinations passed preview checks.")
            else:
                print("RoundCube native import is complete; its verified receipt is retained.")
            if filters is not None and settings is not None:
                filters.run(verify_ready=verify_ready, check_source=check_source)
                if filters.state(filters.users())["filters_complete"]:
                    settings.run(verify_ready=verify_ready, check_source=check_source)
                else:
                    print("Native vacation/forwarding target checks are deferred until editable-filter import completes.")
            check_source()
            verify_ready()
            print("Webmail preview completed; private backups retained, no import or release receipt written and no user data changed.")
            return 0
        print("Private webmail import journal: {}.".format(journal.file))
        phases = (
            ("roundcube", lambda: native.apply(verify_ready=verify_ready, check_source=check_source), native.state, "native_complete"),
        )
        if filters is not None and settings is not None:
            phases += (
                ("filters", lambda: filters.run(verify_ready=verify_ready, check_source=check_source,
                                                apply=True, writers_stopped=writers_stopped),
                 lambda: filters.state(filters.users()), "filters_complete"),
                ("settings", lambda: settings.run(verify_ready=verify_ready, check_source=check_source,
                                                  apply=True, writers_stopped=writers_stopped),
                 lambda: settings.state(reviewed), "settings_complete"),
            )
        receipts: Mapping[str, str] = {}
        for name, operation, read_state, complete in phases:
            journal.write(inputs, name, receipts, deferred)
            operation()
            if read_state().get(complete) is not True:
                raise MigrationError("Webmail " + name + " stage did not complete; retain isolation and its recovery journal")
            receipts = {**receipts, name: journal.receipt(name)}
        journal.write(inputs, "verify", receipts, deferred)
        check_source()
        verify_ready()
        activate_prefilters(context, evidence, reviewed, verify_ready=verify_ready, activate=False)
        if receipts != {name: journal.receipt(name) for name in journal.receipt_names}:
            raise MigrationError("Native import receipts changed during final verification; webmail remains incomplete")
        journal.write(inputs, "complete", receipts, deferred)
        journal.require_complete()
        print("Source webmail stages imported and verified: {}. Target isolation remains paused.".format(", ".join(journal.receipt_names)))
        if deferred:
            print("{} approved preference deferrals remain in the saved RoundCube proposal.".format(deferred))
        print("Review the results before target-isolation --action release --apply. "
              "DNS/source handoff and full migration completion remain separate.")
    return 0
