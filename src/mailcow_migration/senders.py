from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .accounts import account_journal, require_before_activation
from .configuration import Configuration
from .core.errors import ConfigurationError, MigrationError
from .core.state import OperationJournal, Workspace
from .core.storage import read_json
from .plans.policy import load_policy, sender_requests
from .plans.saved import SavedPlan, load_saved_plan
from .targets.mailcow import MailcowTarget
from .targets.reconciliation import api_number, reconcile_pending
from .targets.senders import SECURITY_FIELDS, require_sender_permissions, security_flags, sender_allowed, sender_mailbox, verify_sender_requests
from .transfers import TransferContext, require_mailcow_host


def selected_sender_requests(batch: SavedPlan) -> list:
    requests = sender_requests(batch.plan, load_policy(str(batch.directory / "policy.json")))
    policy = read_json(batch.directory / "account-policy.json", private=True)
    if not isinstance(policy, dict) or policy.get("senderPermissions") != requests:
        raise MigrationError("Sender policy differs from the saved account proposal; reconcile it before changing permissions")
    return requests


def sender_operation(username: str) -> str:
    return "merge sender permissions " + username


def validate_sender_journal(state: Mapping[str, Any], requests: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    selected = {row["username"]: row["senders"] for row in requests}
    operations = {sender_operation(username): username for username in selected}
    records = state.get("requests", [])
    if (type(state.get("version")) is not int or "complete" in state and type(state["complete"]) is not bool
            or not isinstance(records, list) or any(identifier not in operations for identifier in state["done"])):
        raise MigrationError("Invalid sender-permission journal; retain its requests and preserved grants")
    saved = {}
    for row in records:
        if (not isinstance(row, dict) or not isinstance(row.get("username"), str) or row["username"] not in selected
                or row["username"] in saved or row.get("senders") != selected[row["username"]]
                or not isinstance(row.get("preserved"), list) or any(not isinstance(value, str) for value in row["preserved"])):
            raise MigrationError("Invalid preserved sender-permission inventory; retain the journal")
        if "security" in row and (not isinstance(row["security"], dict) or set(row["security"]) != set(SECURITY_FIELDS)
                                  or any(type(value) not in (int, float) or api_number(value) is None for value in row["security"].values())):
            raise MigrationError("Invalid saved mailbox security attributes; retain the sender journal")
        saved[row["username"]] = row
    pending = state.get("pending")
    if state.get("complete") is True and (pending or saved.keys() != selected.keys()):
        raise MigrationError("Sender completion lacks its preserved-grant receipts")
    if pending:
        request = state.get("pendingRequest")
        payload = request.get("payload") if isinstance(request, dict) else None
        attributes = payload.get("attr") if isinstance(payload, dict) else None
        username = operations.get(pending)
        record = saved.get(username)
        if (not isinstance(request, dict) or username is None or record is None or "security" not in record
                or request.get("endpoint") != "edit/mailbox"
                or not isinstance(payload, dict) or payload.get("items") != [username] or not isinstance(attributes, dict)
                or set(attributes) != {"sender_acl", *SECURITY_FIELDS} or not isinstance(attributes["sender_acl"], list)
                or any(not isinstance(value, str) for value in attributes["sender_acl"])
                or not set([*record["preserved"], *selected[username]]) <= set(attributes["sender_acl"])
                or any(attributes[key] != record.get("security", {}).get(key) for key in SECURITY_FIELDS)):
            raise MigrationError("Pending sender request differs from the reviewed scope; no API write was repeated")
    elif "pendingRequest" in state:
        raise MigrationError("Sender request has no pending operation; retain the journal for reconciliation")
    return saved


def sender_journal(context: TransferContext, requests: list) -> OperationJournal:
    return OperationJournal(Workspace(context.batch.directory), "senders.json",
                            {**context.identity, "kind": "mailcow-sender-permissions", "requested": requests})


def verify_senders(context: TransferContext, target: MailcowTarget, inventory: Optional[Mapping[str, list]] = None) -> bool:
    requests = selected_sender_requests(context.batch)
    journal = sender_journal(context, requests)
    state = journal.read()
    validate_sender_journal(state, requests)
    if not requests and not journal.workspace.path(journal.relative_path).exists():
        return False
    if state.get("pending") or state.get("complete") is not True:
        raise MigrationError("Sender permission staging is incomplete or uncertain; reconcile it before verification")
    mailboxes = inventory["mailbox"] if inventory is not None else target.api.inventory("mailbox")
    aliases = inventory["alias"] if inventory is not None else target.api.inventory("alias")
    verify_sender_requests(state["requests"], mailboxes, aliases)
    return True


def run_senders(configuration: Configuration, file: Path, *, apply: bool = False, check: bool = False) -> int:
    require_mailcow_host()
    if apply and check:
        raise ConfigurationError("Use --apply or --check for sender permissions, not both")
    with Workspace().lock():
        batch = load_saved_plan(file, configuration)
        if not check:
            require_before_activation(account_journal(batch, configuration).read())
        context = TransferContext(batch, configuration)
        target = MailcowTarget(configuration.mailcow)
        api = target.api
        if check:
            print("Requested sender permissions and preserved grants passed live verification." if verify_senders(context, target)
                  else "No explicit sender-permission mappings require verification.")
            return 0
        requests = selected_sender_requests(batch)
        journal = sender_journal(context, requests)
        state = journal.read()
        validate_sender_journal(state, requests)
        if apply:
            journal.save({**state, "complete": False})
            print("Private sender-permission journal: {}.".format(journal.workspace.path(journal.relative_path)))
        context.check_accounts(target)
        reconcile_pending(journal, api)
        state = journal.read()
        previous = validate_sender_journal(state, requests)
        mailboxes, aliases = api.inventory("mailbox"), api.inventory("alias")
        prepared = []
        for row in requests:
            username = row["username"]
            mailbox = sender_mailbox(username, mailboxes, inactive=True)
            recorded = previous.get(username)
            preserved = recorded["preserved"] if recorded is not None else list(mailbox["sender_acl"])
            if not set(preserved) <= set(mailbox["sender_acl"]):
                raise MigrationError(username + ": existing sender grants drifted; retain the original journal")
            identifier = sender_operation(username)
            if recorded and recorded.get("security") and identifier in state["done"] and state.get("complete") is not True:
                if security_flags(mailbox) != recorded["security"]:
                    raise MigrationError(username + ": security attributes differ from the unfinished sender edit; reconcile before continuing")
            expected, payload = {**row, "preserved": preserved}, None
            if not all(sender_allowed(mailbox, aliases, sender) for sender in row["senders"]):
                if identifier in state["done"]:
                    raise MigrationError(username + ": completed sender permissions drifted; no write was repeated")
                flags = security_flags(mailbox)
                expected = {**expected, "security": flags}
                payload = {"items": [username], "attr": {"sender_acl": sorted(set([*mailbox["sender_acl"], *row["senders"]])), **flags}}
            prepared.append((expected, payload, list(mailbox["sender_acl"])))
        expected = [row for row, _, _ in prepared]
        if apply:
            journal.save({**journal.read(), "requests": expected, "complete": False})
        for row, payload, before in prepared:
            username = row["username"]
            if payload is None:
                print(username + ": requested permissions already covered; existing grants retained.")
                continue
            if apply:
                current = sender_mailbox(username, api.inventory("mailbox"), inactive=True)
                if set(current["sender_acl"]) != set(before) or security_flags(current) != row["security"]:
                    raise MigrationError(username + ": sender/security attributes changed after preview; no edit attempted")
            result = journal.write_mutation(sender_operation(username), "edit/mailbox", payload, api.request, apply=apply)
            print(username + ": " + result + " sender-permission merge.")
            if apply:
                current = sender_mailbox(username, api.inventory("mailbox"), inactive=True)
                require_sender_permissions(row, current, aliases)
                if security_flags(current) != row["security"]:
                    raise MigrationError(username + ": security attributes changed during the sender edit; journal retained")
        if apply:
            if selected_sender_requests(batch) != requests:
                raise MigrationError("Sender policy changed during staging; completion was not recorded")
            context.check_accounts(target)
            verify_sender_requests(expected, api.inventory("mailbox"), api.inventory("alias"))
            completed = [{key: value for key, value in row.items() if key != "security"} for row in expected]
            journal.save({**journal.read(), "requests": completed, "complete": True})
            print("Sender permissions merged and read back; mailbox activation remains separate.")
        else:
            print("Sender preview completed; no mailbox API writes. Confirmed pending requests may have updated local receipts.")
        print("{} explicit mappings processed; no grants inferred from source aliases.".format(len(requests)))
    return 0
