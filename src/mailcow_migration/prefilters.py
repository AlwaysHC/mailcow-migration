from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple

from .configuration import Configuration
from .core.errors import MigrationError
from .core.state import OperationJournal, Workspace
from .core.storage import json_fingerprint
from .plans.evidence import ExportEvidence, load_plan_export
from .plans.saved import load_saved_plan
from .plans.saved_webmail import SavedWebmail, load_saved_webmail
from .targets.mailcow import MailcowTarget
from .targets.reconciliation import api_number, api_string, reconcile_pending
from .targets.sieve import SieveSource
from .transfers import TransferContext, require_mailcow_host


def matching_prefilter(
    planned: Mapping[str, Any],
    inventory: Sequence[Mapping[str, Any]],
    *,
    required: bool = False,
) -> Optional[Mapping[str, Any]]:
    filters = [
        row
        for row in inventory
        if row.get("username") == planned["username"]
        and row.get("filter_type") == "prefilter"
    ]
    matches = [
        row for row in filters if row.get("script_desc") == planned["script_desc"]
    ]
    if len(matches) > 1 or any(api_number(row.get("active")) != 0 for row in filters):
        raise MigrationError(
            planned["username"]
            + ": active, ambiguous or unrecognized prefilter state; retain and reconcile existing filters"
        )
    found = matches[0] if matches else None
    if found is not None:
        if (
            found.get("script_data") != planned["script_data"]
            or type(found.get("id")) not in (str, int)
            or api_string(found["id"]) == ""
        ):
            raise MigrationError(
                planned["username"]
                + ": existing prefilter differs or has no valid ID; no overwrite performed"
            )
    elif required:
        raise MigrationError(
            planned["username"]
            + ": staged mailbox prefilter is missing; retain its operation journal"
        )
    return found


def validate_prefilter_journal(
    state: Mapping[str, Any],
    operations: Mapping[str, Mapping[str, Any]],
    activation: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> None:
    activation = activation or {}
    requests = {
        **{
            identifier: {"endpoint": "add/filter", "payload": payload}
            for identifier, payload in operations.items()
        },
        **activation,
    }
    if (
        type(state.get("version")) is not int
        or "complete" in state
        and type(state["complete"]) is not bool
        or any(identifier not in requests for identifier in state["done"])
        or any(identifier in activation for identifier in state["done"])
        and state.get("complete") is not True
    ):
        raise MigrationError(
            "Invalid prefilter staging journal; retain its requests and receipts"
        )
    pending = state.get("pending")
    if pending and (
        pending not in requests
        or state.get("pendingRequest") != requests[pending]
        or (state.get("complete") is True) != (pending in activation)
    ):
        raise MigrationError(
            "Pending prefilter request differs from the reviewed plan; no API write was repeated"
        )
    if not pending and "pendingRequest" in state:
        raise MigrationError(
            "Prefilter request has no pending operation; retain the journal for reconciliation"
        )


def prefilter_staging(
    context: TransferContext,
    evidence: ExportEvidence,
    reviewed: SavedWebmail,
) -> Tuple[OperationJournal, Mapping[str, Mapping[str, Any]]]:
    batch, policy, forwarding = context.batch, reviewed.policy, reviewed.forwarding
    filters = reviewed.proposal["prefilters"]
    usernames = {row["username"] for row in batch.plan["mailboxes"]}
    if len({row["username"] for row in filters}) != len(filters) or any(
        row["username"] not in usernames
        or row.get("filter_type") != "prefilter"
        or type(row.get("active")) is not int
        or row["active"] != 0
        for row in filters
    ):
        raise MigrationError(
            "Prefilter staging requires one inactive filter per planned mailbox"
        )
    forwarders = {row["username"] for row in forwarding["forwardings"]}
    operations = {
        ("stage forwarding " if row["username"] in forwarders else "stage vacation ")
        + row["username"]: row
        for row in filters
    }
    identity = {
        **context.identity,
        "kind": "mailcow-prefilter-staging",
        "filters": filters,
        "policy": {
            key: policy.get(key)
            for key in ("vacationZeroIntervalDays", "vacationDatesAreUtc")
        },
        "plannedAt": reviewed.proposal["checked_at"],
        "forwardingsHash": json_fingerprint(forwarding["forwardings"], canonical=True),
        "evidenceHash": SieveSource.read(batch.plan, evidence).fingerprint(),
    }
    return OperationJournal(
        Workspace(batch.directory), "prefilters.json", identity
    ), operations


def prefilter_activations(
    reviewed: SavedWebmail, state: Mapping[str, Any]
) -> Mapping[str, Mapping[str, Any]]:
    filters = reviewed.proposal["prefilters"]
    identifiers = state.get("filter_ids")
    if (
        not isinstance(identifiers, dict)
        or identifiers.keys() != {row["username"] for row in filters}
        or any(
            not isinstance(value, str) or not value for value in identifiers.values()
        )
        or len(set(identifiers.values())) != len(identifiers)
    ):
        raise MigrationError(
            "Prefilter staging lacks its verified target IDs; retain the original journal"
        )
    forwarders = {row["username"]: row for row in reviewed.forwarding["forwardings"]}
    return {
        (
            "activate forwarding "
            if row["username"] in forwarders
            else "activate vacation "
        )
        + row["username"]: {
            "endpoint": "edit/filter",
            "payload": {
                "items": [identifiers[row["username"]]],
                "attr": {
                    "active": forwarders.get(row["username"], {}).get("active", 1)
                },
            },
        }
        for row in filters
    }


def run_prefilters(
    configuration: Configuration, file: Path, exported: Path, *, apply: bool = False
) -> int:
    require_mailcow_host()
    with Workspace().lock():
        batch = load_saved_plan(file, configuration)
        context = TransferContext(batch, configuration)
        context.before_transfer()
        evidence = load_plan_export(configuration, batch, exported)
        reviewed = load_saved_webmail(batch, evidence)
        if reviewed.blockers:
            raise MigrationError(
                "Mailbox filter staging is blocked:\n" + "\n".join(reviewed.blockers)
            )
        filters = reviewed.proposal["prefilters"]
        journal, operations = prefilter_staging(context, evidence, reviewed)
        evidence.verify()
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
        existing = {
            identifier: matching_prefilter(row, inventory)
            for identifier, row in operations.items()
        }
        for identifier, row in operations.items():
            if existing[identifier] is None and identifier in state["done"]:
                raise MigrationError(
                    row["username"]
                    + ": previously written prefilter disappeared; no replacement was created"
                )
        print(
            f"Private prefilter staging journal: {journal.workspace.path(journal.relative_path)}."
        )
        for identifier, row in operations.items():
            if existing[identifier] is not None:
                print(row["username"] + ": matching inactive prefilter retained.")
                continue
            result = journal.write_mutation(
                identifier, "add/filter", row, api.request, apply=apply
            )
            print(row["username"] + ": " + result + " inactive prefilter.")
            if apply:
                matching_prefilter(row, api.inventory("filters"), required=True)
        evidence.verify()
        if apply:
            inventory = api.inventory("filters")
            confirmed = {
                row["username"]: api_string(
                    matching_prefilter(row, inventory, required=True)["id"]
                )
                for row in filters
            }
            if len(set(confirmed.values())) != len(confirmed):
                raise MigrationError(
                    "Target returned duplicate prefilter IDs; retain the staging journal"
                )
            context.check_accounts(target)
            journal.save({**journal.read(), "complete": True, "filter_ids": confirmed})
            print("Forwarding/vacation prefilters staged and read back inactive.")
        else:
            print(
                "Prefilter preview completed; no filter API writes. Confirmed pending requests may have updated local receipts."
            )
        print(
            "Filter activation, native SOGo settings and full migration readiness remain separate."
        )
    return 0
