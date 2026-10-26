import secrets
from pathlib import Path
from typing import Any, Mapping, Sequence, Tuple

from .configuration import Configuration
from .core.errors import ConfigurationError, MigrationError
from .core.state import OperationJournal, Workspace
from .core.storage import local_path, read_json, write_private_json
from .plans.domain import active
from .plans.policy import administrator_name, administrator_plan, load_policy
from .plans.saved import SavedPlan, load_saved_plan
from .targets.admins import domain_administrator, verify_administrator, verify_administrators
from .targets.mailcow import MailcowTarget
from .targets.passwords import verify_target_password
from .targets.reconciliation import api_number, reconcile_pending
from .targets.senders import SECURITY_FIELDS, security_flags
from .transfers import TransferContext, require_mailcow_host


def selected_administrators(batch: SavedPlan) -> Tuple[list, Mapping[str, Any]]:
    policy = load_policy(str(batch.directory / "policy.json"))
    planned = administrator_plan(batch.plan, policy)
    unresolved = [row["id"] + ": " + row["detail"] for row in planned["checks"] if row["status"] == "decision"]
    if unresolved:
        raise MigrationError("Administrator mapping is incomplete:\n" + "\n".join(unresolved))
    saved = read_json(batch.directory / "account-policy.json", private=True)
    if not isinstance(saved, dict) or saved.get("admins") != planned["admins"]:
        raise MigrationError("Administrator policy differs from the saved account proposal; reconcile it before changing permissions")
    return planned["admins"], policy


def administrator_journal(context: TransferContext, planned: list, policy: Mapping[str, Any]) -> OperationJournal:
    return OperationJournal(Workspace(context.batch.directory), "admins.json", {
        **context.identity, "kind": "mailcow-domain-administrators", "admins": planned,
        "policy": {key: policy.get(key) for key in ("adminNames", "allDomainAdmins", "mergeAdmins")},
    })


def administrator_credentials(file: Path, planned: Sequence[Mapping[str, Any]]) -> Mapping[str, str]:
    credentials = read_json(file, private=True) if file.exists() else {}
    names = {row["username"] for row in planned}
    if (not isinstance(credentials, dict) or any(name not in names for name in credentials)
            or any(not isinstance(value, str) or not value for value in credentials.values())):
        raise MigrationError("Invalid private administrator credentials; retain the file without displaying its contents")
    return credentials


def administrator_operations(planned: Sequence[Mapping[str, Any]]) -> Mapping[str, Tuple[str, Mapping[str, Any]]]:
    return {action + " administrator " + row["username"]: (action, row) for row in planned for action in ("create", "merge")}


def validate_administrator_journal(
    state: Mapping[str, Any], planned: Sequence[Mapping[str, Any]], credentials: Mapping[str, str],
) -> None:
    operations = administrator_operations(planned)
    users = {row["username"]: row for row in planned}
    expected = state.get("expectations", {})
    if (type(state.get("version")) is not int or "complete" in state and type(state["complete"]) is not bool
            or not isinstance(expected, dict) or any(name not in users for name in expected)
            or any(identifier not in operations for identifier in state["done"])):
        raise MigrationError("Invalid administrator import journal; retain its ownership and membership receipts")
    for username, row in expected.items():
        if (not isinstance(row, dict) or any(not isinstance(row.get(key), list) or any(not isinstance(value, str) for value in row[key])
                                            for key in ("preserved", "domains"))
                or not set([*row["preserved"], *users[username]["domains"]]) <= set(row["domains"])
                or "security" in row and (not isinstance(row["security"], dict) or set(row["security"]) != set(SECURITY_FIELDS)
                                          or any(type(value) not in (int, float) or api_number(value) is None
                                                 for value in row["security"].values()))):
            raise MigrationError(username + ": invalid saved administrator memberships/security attributes")
    if any(operations[name][1]["username"] not in expected for name in state["done"]):
        raise MigrationError("Administrator operation lacks its preserved-membership receipt")
    for username in users:
        if all(action + " administrator " + username in state["done"] for action in ("create", "merge")):
            raise MigrationError(username + ": inconsistent administrator ownership receipts")
    pending = state.get("pending")
    if state.get("complete") is True and (pending or expected.keys() != users.keys()):
        raise MigrationError("Administrator completion lacks all membership receipts")
    if pending:
        if pending not in operations or operations[pending][1]["username"] not in expected:
            raise MigrationError("Unknown pending administrator operation; retain the journal")
        action, row = operations[pending]
        receipt = expected[row["username"]]
        if action == "create":
            password = credentials.get(row["username"])
            if password is None or receipt["preserved"]:
                raise MigrationError("Pending administrator creation lacks its original private credentials or conflicts with ownership")
            request = {"endpoint": "add/domain-admin", "payload": {**row, "password": password, "password2": password}}
        else:
            if "security" not in receipt:
                raise MigrationError("Pending administrator merge lacks preserved security attributes")
            request = {"endpoint": "edit/domain-admin", "payload": {"items": [row["username"]], "attr": {
                "domains": receipt["domains"], "active": row["active"], **receipt["security"],
            }}}
        if state.get("pendingRequest") != request:
            raise MigrationError("Pending administrator request differs from the reviewed scope; no write was repeated")
    elif "pendingRequest" in state:
        raise MigrationError("Administrator request has no pending identity; retain the journal")


def verify_admins(context: TransferContext, target: MailcowTarget) -> bool:
    planned, policy = selected_administrators(context.batch)
    journal = administrator_journal(context, planned, policy)
    state = journal.read()
    if not planned and not journal.workspace.path(journal.relative_path).exists():
        return False
    if state.get("pending") or state.get("complete") is not True:
        raise MigrationError("Complete admins --apply after activation before webmail import; reconcile any pending write")
    validate_administrator_journal(state, planned, {})
    verify_administrators(planned, state["expectations"], target.api.inventory("domain-admin"))
    return True


def run_admins(configuration: Configuration, file: Path, *, apply: bool = False, check: bool = False) -> int:
    require_mailcow_host()
    if apply and check:
        raise ConfigurationError("Use --apply or --check for domain administrators, not both")
    with Workspace().lock():
        batch = load_saved_plan(file, configuration)
        context, target = TransferContext(batch, configuration), MailcowTarget(configuration.mailcow)
        if check:
            print("Domain administrator grants and preserved memberships verified." if verify_admins(context, target)
                  else "No domain administrators require import; global/panel administrators remain outside this step.")
            return 0
        planned, policy = selected_administrators(batch)
        excluded = sum(bool(active(row.get("superadmin"))) for row in batch.plan["source"]["admins"])
        if excluded:
            print("{} global administrators excluded; review their shared-server mappings separately.".format(excluded))
        journal = administrator_journal(context, planned, policy)
        credentials_file = local_path(batch.directory / "admin-credentials.json")
        credentials = administrator_credentials(credentials_file, planned)
        state = journal.read()
        validate_administrator_journal(state, planned, credentials)
        if apply:
            context.check_accounts(target, activated=True)
            journal.save({**state, "complete": False})
        reconcile_pending(journal, target.api, lambda kind, payload, password: verify_target_password(
            target, kind, payload, password, batch.directory,
        ))
        state = journal.read()
        validate_administrator_journal(state, planned, credentials)
        inventory, prepared = target.api.inventory("domain-admin"), []
        for row in planned:
            username = row["username"]
            found = domain_administrator(row, inventory)
            prior = state.get("expectations", {}).get(username)
            create, merge = "create administrator " + username, "merge administrator " + username
            done = create in state["done"] or merge in state["done"]
            renamed = any(not active(source.get("superadmin")) and source["username"] != username
                          and administrator_name(source["username"], policy) == username for source in batch.plan["source"]["admins"])
            if found is None:
                if done or prior and (prior["preserved"] or "security" in prior):
                    raise MigrationError(username + ": administrator disappeared; no replacement or password reset attempted")
                prepared.append((row, {"preserved": [], "domains": row["domains"]}, create, None, None))
                continue
            if renamed and username not in policy.get("mergeAdmins", []) and create not in state["done"]:
                raise MigrationError(username + ": mapped administrator already exists; confirm its owner through mergeAdmins before continuing")
            before = found["selected_domains"]
            preserved = prior["preserved"] if prior is not None else list(before)
            if not set(preserved) <= set(before):
                raise MigrationError(username + ": preserved administrator memberships drifted; retain the journal")
            if prior and "security" in prior and merge in state["done"]:
                verify_administrator(row, prior, found)
            if set(row["domains"]) <= set(before):
                prepared.append((row, {"preserved": preserved, "domains": before}, None, None, before))
                continue
            if done:
                raise MigrationError(username + ": completed administrator grants drifted; no write repeated")
            if username not in policy.get("mergeAdmins", []):
                raise MigrationError(username + ": adding domain membership requires an explicit mergeAdmins ownership decision")
            expected = {"preserved": list(before), "domains": list(dict.fromkeys([*before, *row["domains"]])), "security": security_flags(found)}
            payload = {"items": [username], "attr": {"domains": expected["domains"], "active": row["active"], **expected["security"]}}
            prepared.append((row, expected, merge, payload, before))
        if apply:
            missing = [row["username"] for row, _, identifier, _, before in prepared
                       if identifier is not None and before is None and row["username"] not in credentials]
            if missing:
                credentials = {**credentials, **{username: "Aa1!" + secrets.token_urlsafe(24) for username in missing}}
                write_private_json(credentials_file, credentials, replace=True)
            journal.save({**journal.read(), "expectations": {row["username"]: expected for row, expected, _, _, _ in prepared}, "complete": False})
        print("Private administrator journal: {}.".format(journal.workspace.path(journal.relative_path)))
        for row, expected, identifier, payload, before in prepared:
            username = row["username"]
            if not apply:
                print("{}: active={}; domains={}.".format(username, row["active"], ", ".join(expected["domains"])))
            if identifier is None:
                print(username + ": requested administrator membership already covered.")
                continue
            if before is None:
                password = credentials[username] if apply else "preview-only"
                payload = {**row, "password": password, "password2": password}
            if apply:
                current = domain_administrator(row, target.api.inventory("domain-admin"))
                if (before is None and current is not None or before is not None and (
                        current is None or set(current["selected_domains"]) != set(before) or security_flags(current) != expected["security"])):
                    raise MigrationError(username + ": administrator changed after preflight; no edit attempted")
            endpoint = "add/domain-admin" if before is None else "edit/domain-admin"
            result = journal.write_mutation(identifier, endpoint, payload, target.api.request, apply=apply)
            print(identifier + ": " + result + ".")
            if apply:
                verify_administrator(row, expected, domain_administrator(row, target.api.inventory("domain-admin")), exact=True)
        if apply:
            if selected_administrators(batch) != (planned, policy):
                raise MigrationError("Administrator policy changed during import; completion was not recorded")
            verify_administrators(planned, journal.read()["expectations"], target.api.inventory("domain-admin"), exact=True)
            completed = {name: {key: value for key, value in row.items() if key != "security"}
                         for name, row in journal.read()["expectations"].items()}
            journal.save({**journal.read(), "expectations": completed, "complete": True})
            print("Domain administrator memberships imported and read back. Private credentials, if generated: {}.".format(credentials_file))
        else:
            print("Administrator preview completed; no API writes or generated passwords. Confirmed requests may have updated local receipts.")
    return 0
