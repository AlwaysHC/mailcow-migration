from typing import Any, Callable, Mapping, Sequence

from .mailcow import MailcowTarget
from .reconciliation import api_number, api_string, reconcile_pending
from .sogo_settings import SogoSettingsImport, handoff_prefilter
from ..core.errors import MigrationError
from ..plans.evidence import ExportEvidence
from ..plans.saved_webmail import SavedWebmail, load_saved_webmail
from ..prefilters import prefilter_activations, prefilter_staging, validate_prefilter_journal
from ..sources import SourceKind
from ..transfers import TransferContext, require_mailcow_host


def activation_inventory(
    reviewed: SavedWebmail, state: Mapping[str, Any], operations: Mapping[str, Mapping[str, Any]],
    native: Mapping[str, Any], inventory: Sequence[Mapping[str, Any]],
) -> Mapping[str, Mapping[str, Any]]:
    by_identifier = {request["payload"]["items"][0]: identifier for identifier, request in operations.items()}
    found = {}
    for planned in reviewed.proposal["prefilters"]:
        username = planned["username"]
        imported = native.get(username)
        expected = (imported.get("prefilter") or planned) if imported is not None else planned
        row = handoff_prefilter({"username": username}, expected, inventory)
        if row is None or api_string(row["id"]) != state["filter_ids"][username]:
            raise MigrationError(username + ": staged prefilter ID changed; no activation write attempted")
        identifier = by_identifier[state["filter_ids"][username]]
        actual = api_number(row.get("active"))
        if imported is not None:
            if state.get("pending") == identifier:
                raise MigrationError(username + ": uncertain prefilter activation overlaps native settings; reconcile both journals")
            desired = (imported.get("prefilter") or {}).get("active", 0)
            if actual != api_number(desired):
                raise MigrationError(username + ": prefilter activity differs after native settings import")
        else:
            desired = operations[identifier]["payload"]["attr"]["active"]
            allowed = (0, desired) if state.get("pending") == identifier else (desired if identifier in state["done"] else 0,)
            if actual not in allowed:
                raise MigrationError(username + ": prefilter activity differs from its activation receipt")
        found[identifier] = row
    return found


def activate_prefilters(
    context: TransferContext, evidence: ExportEvidence, reviewed: SavedWebmail, *,
    verify_ready: Callable[[], None], apply: bool = False, activate: bool = True,
) -> None:
    """Caller holds the shared lock; verify_ready checks activation phase, stopped writers and source/target isolation."""
    require_mailcow_host()
    if context.batch.source.kind is not SourceKind.POSTFIXADMIN:
        raise MigrationError("Prefilter activation requires the implemented PostfixAdmin isolation workflow")
    if apply and not activate:
        raise MigrationError("Prefilter verification cannot request writes")
    if reviewed.blockers:
        raise MigrationError("Resolve the saved webmail findings before activation:\n" + "\n".join(reviewed.blockers))
    evidence.verify()
    journal, staged = prefilter_staging(context, evidence, reviewed)
    if not journal.workspace.path(journal.relative_path).exists():
        if reviewed.forwarding["forwardings"] or any(
                api_number(row.get("active")) == 1 for row in (context.batch.plan["source"].get("vacations") or [])):
            raise MigrationError("Mailbox forwarding/vacations have no staging record; finish prefilter staging before activation")
        print("No staged mailbox prefilters require activation.")
        return
    state = journal.read()
    if state.get("complete") is not True:
        raise MigrationError("Complete mailbox prefilter staging before activation")
    operations = prefilter_activations(reviewed, state)
    validate_prefilter_journal(state, staged, operations)
    if activate:
        transfer = context.final_state()
        if operations and transfer.get("sieveEvidenceHash") != state["evidenceHash"]:
            raise MigrationError("Copied Sieve differs from staged mailbox filters; repeat Sieve and prefilter review before activation")
    settings = SogoSettingsImport.from_export(context.configuration, context.batch, evidence.directory)
    native = settings.state(reviewed)["users"] if settings.file.exists() else {}
    verify_ready()
    api = MailcowTarget(context.configuration.mailcow).api
    found = activation_inventory(reviewed, state, operations, native, api.inventory("filters"))
    if state.get("pending"):
        if not activate:
            raise MigrationError("Uncertain prefilter activation must be reconciled before read-only verification")
        reconcile_pending(journal, api)
        state = journal.read()
        validate_prefilter_journal(state, staged, operations)
        found = activation_inventory(reviewed, state, operations, native, api.inventory("filters"))
    if not activate:
        print("Staged/native mailbox prefilters match their recorded activity; no writes performed.")
        return
    for identifier, request in operations.items():
        if apply:
            verify_ready()
            found = activation_inventory(reviewed, journal.read(), operations, native, api.inventory("filters"))
        row = found[identifier]
        if row["username"] in native:
            print(row["username"] + ": native settings handoff verified; prefilter activity retained.")
            continue
        outcome = journal.write_mutation(identifier, request["endpoint"], request["payload"], api.request, apply=apply)
        print(identifier + ": " + outcome + ".")
        if apply:
            activation_inventory(reviewed, journal.read(), operations, native, api.inventory("filters"))
    evidence.verify()
    current = load_saved_webmail(context.batch, evidence)
    confirmed, current_staging = prefilter_staging(context, evidence, current)
    final = confirmed.read()
    validate_prefilter_journal(final, current_staging, prefilter_activations(current, final))
    if current.proposal["nativeSettings"] != reviewed.proposal["nativeSettings"]:
        raise MigrationError("Native settings proposal changed during prefilter activation; completion was not confirmed")
    verify_ready()
    activation_inventory(current, final, operations, native, api.inventory("filters"))
    print("Mailbox prefilter activation completed and read back." if apply else
          "Mailbox prefilter preview completed; no API writes. Confirmed pending requests may have updated local receipts.")
