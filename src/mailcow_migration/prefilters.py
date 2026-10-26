from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .configuration import Configuration
from .core.errors import MigrationError
from .core.state import OperationJournal, Workspace
from .core.storage import json_fingerprint, read_json
from .plans.evidence import load_plan_export
from .plans.forwarding import mailbox_forwardings
from .plans.policy import load_policy
from .plans.saved import load_saved_plan
from .plans.sieve_evidence import load_sieve_evidence
from .plans.webmail import webmail_assessment
from .targets.mailcow import MailcowTarget
from .targets.reconciliation import api_number, api_string, reconcile_pending
from .targets.sieve import SieveSource
from .transfers import TransferContext, require_mailcow_host


def matching_prefilter(
    planned: Mapping[str, Any], inventory: Sequence[Mapping[str, Any]], *, required: bool = False,
) -> Optional[Mapping[str, Any]]:
    filters = [row for row in inventory if row.get("username") == planned["username"] and row.get("filter_type") == "prefilter"]
    matches = [row for row in filters if row.get("script_desc") == planned["script_desc"]]
    if len(matches) > 1 or any(api_number(row.get("active")) != 0 for row in filters):
        raise MigrationError(planned["username"] + ": active, ambiguous or unrecognized prefilter state; retain and reconcile existing filters")
    found = matches[0] if matches else None
    if found is not None:
        if (found.get("script_data") != planned["script_data"] or type(found.get("id")) not in (str, int)
                or api_string(found["id"]) == ""):
            raise MigrationError(planned["username"] + ": existing prefilter differs or has no valid ID; no overwrite performed")
    elif required:
        raise MigrationError(planned["username"] + ": staged mailbox prefilter is missing; retain its operation journal")
    return found


def validate_prefilter_journal(state: Mapping[str, Any], operations: Mapping[str, Mapping[str, Any]]) -> None:
    if (type(state.get("version")) is not int or "complete" in state and type(state["complete"]) is not bool
            or any(identifier not in operations for identifier in state["done"])):
        raise MigrationError("Invalid prefilter staging journal; retain its requests and receipts")
    pending = state.get("pending")
    if pending and (state.get("complete") is True or pending not in operations or state.get("pendingRequest") != {
        "endpoint": "add/filter", "payload": operations.get(pending),
    }):
        raise MigrationError("Pending prefilter request differs from the reviewed plan; no API write was repeated")
    if not pending and "pendingRequest" in state:
        raise MigrationError("Prefilter request has no pending operation; retain the journal for reconciliation")


def run_prefilters(configuration: Configuration, file: Path, exported: Path, *, apply: bool = False) -> int:
    require_mailcow_host()
    with Workspace().lock():
        batch = load_saved_plan(file, configuration)
        context = TransferContext(batch, configuration)
        context.before_transfer()
        evidence = load_plan_export(configuration, batch, exported)
        saved = read_json(batch.directory / "webmail-plan.json", private=True)
        if (not isinstance(saved, dict) or saved.get("version") != 1 or saved.get("kind") != "source-webmail-plan"
                or saved.get("target_verified") is not False or not isinstance(saved.get("checked_at"), str)):
            raise MigrationError("Missing or invalid saved webmail proposal; retain the planning evidence")
        try:
            planned_at = datetime.fromisoformat(saved["checked_at"])
        except ValueError as error:
            raise MigrationError("Invalid saved webmail planning time") from error
        if planned_at.tzinfo is None:
            raise MigrationError("Saved webmail planning time must include its timezone")
        policy = load_policy(str(batch.directory / "policy.json"))
        forwarding = mailbox_forwardings(batch.plan)
        scripts = load_sieve_evidence(batch.plan, evidence)
        planned = webmail_assessment(batch.plan, scripts, policy, batch.source.kind, forwarding["forwardings"], now=planned_at)
        if json_fingerprint(saved, canonical=True) != json_fingerprint(planned, canonical=True):
            raise MigrationError("Saved webmail proposal differs from its export or policy; reconcile it before staging filters")
        blockers = [row["id"] + ": " + row["detail"] for row in (*forwarding["checks"], *planned["checks"])
                    if row["status"] in ("blocked", "decision")]
        if blockers:
            raise MigrationError("Mailbox filter staging is blocked:\n" + "\n".join(blockers))
        filters = planned["prefilters"]
        usernames = {row["username"] for row in batch.plan["mailboxes"]}
        if (len({row["username"] for row in filters}) != len(filters)
                or any(row["username"] not in usernames or row.get("filter_type") != "prefilter"
                       or type(row.get("active")) is not int or row["active"] != 0 for row in filters)):
            raise MigrationError("Prefilter staging requires one inactive filter per planned mailbox")
        forwarders = {row["username"] for row in forwarding["forwardings"]}
        operations = {("stage forwarding " if row["username"] in forwarders else "stage vacation ") + row["username"]: row for row in filters}
        identity = {**context.identity, "kind": "mailcow-prefilter-staging", "filters": filters,
                    "policy": {key: policy.get(key) for key in ("vacationZeroIntervalDays", "vacationDatesAreUtc")},
                    "plannedAt": saved["checked_at"], "forwardingsHash": json_fingerprint(forwarding["forwardings"], canonical=True),
                    "evidenceHash": SieveSource.read(batch.plan, evidence).fingerprint()}
        evidence.verify()
        journal = OperationJournal(Workspace(batch.directory), "prefilters.json", identity)
        state = journal.read()
        validate_prefilter_journal(state, operations)
        if apply:
            journal.save({**state, "complete": False})
        target = MailcowTarget(configuration.mailcow)
        context.check_accounts(target)
        api = target.api
        reconcile_pending(journal, api)
        state = journal.read()
        validate_prefilter_journal(state, operations)
        inventory = api.inventory("filters")
        existing = {identifier: matching_prefilter(row, inventory) for identifier, row in operations.items()}
        for identifier, row in operations.items():
            if existing[identifier] is None and identifier in state["done"]:
                raise MigrationError(row["username"] + ": previously written prefilter disappeared; no replacement was created")
        print("Private prefilter staging journal: {}.".format(journal.workspace.path(journal.relative_path)))
        for identifier, row in operations.items():
            if existing[identifier] is not None:
                print(row["username"] + ": matching inactive prefilter retained.")
                continue
            result = journal.write_mutation(identifier, "add/filter", row, api.request, apply=apply)
            print(row["username"] + ": " + result + " inactive prefilter.")
            if apply:
                matching_prefilter(row, api.inventory("filters"), required=True)
        evidence.verify()
        if apply:
            inventory = api.inventory("filters")
            confirmed = {row["username"]: api_string(matching_prefilter(row, inventory, required=True)["id"]) for row in filters}
            if len(set(confirmed.values())) != len(confirmed):
                raise MigrationError("Target returned duplicate prefilter IDs; retain the staging journal")
            context.check_accounts(target)
            journal.save({**journal.read(), "complete": True, "filter_ids": confirmed})
            print("Forwarding/vacation prefilters staged and read back inactive.")
        else:
            print("Prefilter preview completed; no filter API writes. Confirmed pending requests may have updated local receipts.")
        print("Filter activation, native SOGo settings and full migration readiness remain separate.")
    return 0
