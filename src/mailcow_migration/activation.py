from pathlib import Path
from typing import Any, Mapping

from .accounts import (
    account_activation_mutations, account_journal, account_mutations, account_requests, validate_account_journal, verify_account_inventory,
)
from .configuration import Configuration
from .core.errors import ConfigurationError, MigrationError
from .core.state import Workspace
from .plans.evidence import load_plan_export
from .plans.recovery import recovery_plan
from .plans.saved import load_saved_plan
from .plans.saved_webmail import load_saved_webmail
from .senders import verify_senders
from .sources import SourceKind
from .targets.account_inventory import account_inventory
from .targets.mailcow import MailcowTarget
from .targets.prefilter_activation import activate_prefilters
from .targets.reconciliation import reconcile_pending
from .transfers import TransferContext, require_transfer_host


def run_activation(
    configuration: Configuration, file: Path, exported: Path, *, apply: bool = False, ready: bool = False, writers_stopped: bool = False,
) -> int:
    require_transfer_host(writers_stopped)
    if not ready:
        raise ConfigurationError("Activation requires --ready after reviewing the batch and completing its cutover prerequisites")
    with Workspace().lock():
        batch = load_saved_plan(file, configuration)
        if batch.source.kind is not SourceKind.POSTFIXADMIN:
            raise MigrationError("Account activation requires implemented source isolation; the Plesk adapter remains pending")
        if batch.blocking_findings:
            raise MigrationError("Resolve saved planning findings before activation:\n" + batch.blocking_findings)
        context = TransferContext(batch, configuration)
        evidence = load_plan_export(configuration, batch, exported)
        reviewed = load_saved_webmail(batch, evidence)
        if reviewed.blockers:
            raise MigrationError("Resolve saved webmail findings before activation:\n" + "\n".join(reviewed.blockers))
        target = MailcowTarget(configuration.mailcow)
        journal = account_journal(batch, configuration)
        state = journal.read()
        if state.get("accounts") is not True:
            raise MigrationError("Complete account staging before activation")
        if state.get("pending") and "activationRequests" not in state:
            raise MigrationError("Reconcile the uncertain account-staging request before activation")
        recovery = recovery_plan(batch.plan)["emails"]
        staging = account_mutations(batch.plan, recovery, reuse_domain=bool(state.get("existingDomain")))
        inventory = account_inventory(target.api)
        operations = account_activation_mutations(batch.plan, inventory)
        requests = account_requests(operations)
        validate_account_journal(state, staging, operations if "activationRequests" in state else ())
        verify_account_inventory(batch.plan, inventory, recovery, state)
        context.final_state()
        if state.get("activated"):
            print("Batch already activated; its account inventory matches the plan. Webmail import and release are separate.")
            return 0
        context.check_holds(target)
        reconcile_pending(journal, target.api)

        def verify_inventory() -> Mapping[str, Any]:
            current = journal.read()
            current_inventory = account_inventory(target.api)
            if account_requests(account_activation_mutations(batch.plan, current_inventory)) != requests:
                raise MigrationError("Target activation IDs changed after preflight; retain the pinned requests")
            validate_account_journal(current, staging, operations if "activationRequests" in current else ())
            if current.get("pending"):
                raise MigrationError("Reconcile the uncertain account activation before another write")
            verify_account_inventory(batch.plan, current_inventory, recovery, current)
            verify_senders(context, target, current_inventory)
            return current

        def verify_ready() -> None:
            context.final_state()
            verify_inventory()
            context.check_holds(target)

        verify_ready()
        activate_prefilters(context, evidence, reviewed, verify_ready=verify_ready)
        if apply:
            evidence.verify()
            journal.save({**journal.read(), "activationRequests": requests})
        print("Account activation journal: {}.".format(journal.workspace.path(journal.relative_path)))
        print("Activation preserves target access/delivery isolation; source logins and delivery remain held.")
        for index, operation in enumerate(operations):
            verify_ready()
            result = journal.write_mutation(operation.identifier, operation.endpoint, operation.payload, target.api.request, apply=apply)
            print("{}/{}: {}: {}.".format(index + 1, len(operations), operation.identifier, result))
            if apply:
                verify_inventory()
            if index == 0:
                activate_prefilters(context, evidence, reviewed, verify_ready=verify_ready, apply=apply)
        evidence.verify()
        activate_prefilters(context, evidence, load_saved_webmail(batch, evidence), verify_ready=verify_ready, activate=False)
        verify_ready()
        if apply:
            journal.save({**journal.read(), "activated": True})
            print("Planned account activity restored and read back. Complete webmail import before releasing target isolation.")
        else:
            print("Activation preview completed; no API writes. Confirmed pending requests may have updated local receipts.")
    return 0
