import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Tuple

from .configuration import Configuration
from .core.errors import ConfigurationError, MigrationError
from .core.state import OperationJournal, Workspace
from .core.storage import local_path, write_private_json
from .plans.domain import MIB
from .plans.forwarding import mailbox_forwardings
from .plans.recovery import recovery_plan
from .plans.saved import SavedPlan, load_saved_plan
from .plans.security import account_security_policy
from .plans.server_state import require_server_settings
from .targets.account_inventory import DOMAIN_FIELDS, account_conflicts, account_inventory, domain_value, native_route_conflicts
from .targets.mailcow import MailcowTarget
from .targets.passwords import check_password_support, verify_mailbox_passwords, verify_target_password
from .targets.reconciliation import alias_destinations, api_number, api_string, reconcile_pending


@dataclass(frozen=True)
class AccountMutation:
    identifier: str
    endpoint: str
    payload: Mapping[str, Any] = field(repr=False)


def account_mutations(plan: Mapping[str, Any], recovery: list, *, reuse_domain: bool) -> Tuple[AccountMutation, ...]:
    domain = plan["domain"]
    first = (AccountMutation("configure existing empty domain", "edit/domain", {
        "items": [domain], "attr": {key: value for key, value in plan["domainPayload"].items() if key != "domain"},
    }) if reuse_domain else AccountMutation("create domain", "add/domain", plan["domainPayload"]))
    return (
        first,
        *(AccountMutation("create mailbox " + row["username"], "add/mailbox", row["payload"]) for row in plan["mailboxes"]),
        *(AccountMutation(row["operation"], "edit/mailbox", {"items": [row["username"]], "attr": {"pw_recovery_email": row["email"]}})
          for row in recovery),
        *(AccountMutation("create alias domain " + row["alias_domain"], "add/alias-domain", {
            "alias_domain": row["alias_domain"], "target_domain": row["target_domain"], "active": 0,
        }) for row in plan["aliasDomains"]),
        *(AccountMutation("create alias " + row["address"], "add/alias", row["payload"]) for row in plan["aliases"] if not row["mailbox"]),
        *(AccountMutation("create BCC " + row["local_dest"], "add/bcc", row) for row in plan["bcc"]),
        AccountMutation("stage domain disabled", "edit/domain", {"items": [domain], "attr": {"active": 0}}),
    )


def account_requests(operations: Tuple[AccountMutation, ...]) -> Mapping[str, Any]:
    expected = {row.identifier: {"endpoint": row.endpoint, "payload": row.payload} for row in operations}
    if len(expected) != len(operations):
        raise MigrationError("Plan repeats an account operation identity; resolve the source mappings")
    return expected


def activation_flag(value: Any) -> int:
    number = api_number(value)
    if number not in (0, 1):
        raise MigrationError("Account activation requires a valid planned activity flag")
    return int(number)


def account_activation_mutations(plan: Mapping[str, Any], inventory: Mapping[str, list]) -> Tuple[AccountMutation, ...]:
    def target_id(kind: str, fields: Mapping[str, Any]) -> str:
        matches = [row for row in inventory[kind] if all(row.get(key) == value for key, value in fields.items())]
        if len(matches) != 1 or type(matches[0].get("id")) not in (str, int) or not api_string(matches[0]["id"]):
            raise MigrationError("Activation requires one target {} with a valid ID: {}".format(kind, fields))
        return api_string(matches[0]["id"])

    for row in plan["mailboxes"]:
        if sum(item.get("username") == row["username"] for item in inventory["mailbox"]) != 1:
            raise MigrationError("Activation requires one target mailbox: " + row["username"])
    for row in plan["aliasDomains"]:
        if sum(item.get("alias_domain") == row["alias_domain"] for item in inventory["alias-domain"]) != 1:
            raise MigrationError("Activation requires one target alias domain: " + row["alias_domain"])
    if sum((row.get("domain_name") or row.get("domain")) == plan["domain"] for row in inventory["domain"]) != 1:
        raise MigrationError("Activation requires one target primary domain: " + plan["domain"])
    return (
        AccountMutation("enable domain for activation", "edit/domain", {"items": [plan["domain"]], "attr": {"active": 1}}),
        *(AccountMutation("activate mailbox " + row["username"], "edit/mailbox", {
            "items": [row["username"]], "attr": {"active": activation_flag(row["active"])},
        }) for row in plan["mailboxes"]),
        *(AccountMutation("activate alias domain " + row["alias_domain"], "edit/alias-domain", {
            "items": [row["alias_domain"]], "attr": {"active": activation_flag(row["active"])},
        }) for row in plan["aliasDomains"]),
        *(AccountMutation("activate alias " + row["address"], "edit/alias", {
            "items": [target_id("alias", {"address": row["address"]})], "attr": {"active": activation_flag(row["active"])},
        }) for row in plan["aliases"] if not row["mailbox"]),
        *(AccountMutation("activate BCC " + row["local_dest"], "edit/bcc", {
            "items": [target_id("bcc", {key: row[key] for key in ("local_dest", "bcc_dest", "type")})], "attr": {"active": 1},
        }) for row in plan["bcc"]),
        AccountMutation("restore domain state", "edit/domain", {
            "items": [plan["domain"]], "attr": {"active": activation_flag(plan["domainActive"])},
        }),
    )


def validate_account_journal(
    state: Mapping[str, Any], operations: Tuple[AccountMutation, ...], activation: Tuple[AccountMutation, ...] = (),
) -> None:
    expected = account_requests((*operations, *activation))
    if any(key in state and type(state[key]) is not bool for key in ("accounts", "activated")):
        raise MigrationError("Invalid account completion markers; retain the journal")
    if any(name not in expected for name in state["done"]):
        raise MigrationError("The journal contains operations outside the selected account phase; retain it for the matching workflow")
    if state.get("pending") and (state["pending"] not in expected or state.get("pendingRequest") != expected[state["pending"]]):
        raise MigrationError("The pending request differs from this account plan; no reconciliation or write was attempted")
    if "pendingRequest" in state and not state.get("pending"):
        raise MigrationError("The journal has a request without an operation identity; retain it for reconciliation")
    if state.get("accounts") and any(row.identifier not in state["done"] for row in operations):
        raise MigrationError("The account-completion marker is inconsistent with the saved operations")
    if activation:
        if state.get("accounts") is not True or state.get("activationRequests") != account_requests(activation):
            raise MigrationError("Saved activation requests differ from the staged plan or target IDs; no write was repeated")
        identifiers = [row.identifier for row in activation]
        done = [identifier for identifier in state["done"] if identifier in identifiers]
        pending = state.get("pending")
        if (done != identifiers[:len(done)] or pending and (len(done) == len(identifiers) or pending != identifiers[len(done)])
                or state.get("activated") and (pending or done != identifiers)):
            raise MigrationError("Account activation order or completion differs from its receipts; retain the journal")
    elif "activationRequests" in state or state.get("activated"):
        raise MigrationError("Use the activation workflow to validate this account journal")


def account_activity_matches(state: Mapping[str, Any], identifier: str, actual: Any, desired: Any, before: int = 0) -> bool:
    desired = activation_flag(desired)
    expected = desired if state.get("activated") or identifier in state["done"] else before
    allowed = (expected, desired) if state.get("pending") == identifier else (expected,)
    return api_number(actual) in allowed


def verify_recovery_emails(emails: list, mailboxes: list) -> None:
    for row in emails:
        mailbox = next((item for item in mailboxes if item.get("username") == row["username"]), {})
        attributes = mailbox.get("attributes") if isinstance(mailbox.get("attributes"), dict) else {}
        if attributes.get("recovery_email") != row["email"]:
            raise MigrationError("Recovery email mismatch for {}; verify target support for pw_recovery_email".format(row["username"]))


def verify_account_inventory(plan: Mapping[str, Any], inventory: Mapping[str, list], recovery: list, state: Mapping[str, Any]) -> None:
    failures = []
    primary = next((row for row in inventory["domain"] if (row.get("domain_name") or row.get("domain")) == plan["domain"]), {})
    restoring = state.get("activated") or "restore domain state" in state["done"] or state.get("pending") == "restore domain state"
    if not primary or not account_activity_matches(
        state, "restore domain state" if restoring else "enable domain for activation", primary.get("active"),
        plan["domainActive"] if restoring else 1, 1 if restoring else 0,
    ):
        failures.append("domain " + plan["domain"])
    for key, column in DOMAIN_FIELDS.items():
        if primary.get(column) is None or api_number(domain_value(primary, key)) != plan["domainPayload"][key]:
            failures.append("domain {}: {}".format(plan["domain"], column))
    for row in plan["mailboxes"]:
        found = next((item for item in inventory["mailbox"] if item.get("username") == row["username"]), {})
        if (not found or found.get("name") != row["payload"]["name"]
                or not account_activity_matches(state, "activate mailbox " + row["username"], found.get("active"), row["active"])
                or api_number(found.get("quota")) != row["payload"]["quota"] * MIB):
            failures.append("mailbox " + row["username"])
        attributes = found.get("attributes") if isinstance(found.get("attributes"), dict) else {}
        for key in ("quarantine_notification", "quarantine_category"):
            if key in row["payload"] and attributes.get(key) != row["payload"][key]:
                failures.append("mailbox {}: {}".format(row["username"], key))
    for row in plan["aliasDomains"]:
        found = next((item for item in inventory["alias-domain"] if item.get("alias_domain") == row["alias_domain"]), {})
        if (not found or found.get("target_domain") != row["target_domain"]
                or not account_activity_matches(state, "activate alias domain " + row["alias_domain"], found.get("active"), row["active"])):
            failures.append("alias domain " + row["alias_domain"])
    if native_route_conflicts(plan, inventory["alias"]):
        failures.append("separate aliases conflict with native mailbox delivery")
    for row in plan["aliases"]:
        if row["mailbox"]:
            continue
        found = next((item for item in inventory["alias"] if item.get("address") == row["address"]), {})
        destination = "null@localhost" if row["payload"].get("goto_null") else row["payload"].get("goto")
        if (not found or alias_destinations(found.get("goto")) != alias_destinations(destination)
                or not account_activity_matches(state, "activate alias " + row["address"], found.get("active"), row["active"])):
            failures.append("alias " + row["address"])
    for row in plan["bcc"]:
        matches = [item for item in inventory["bcc"] if all(item.get(key) == row[key] for key in ("local_dest", "bcc_dest", "type"))]
        if len(matches) != 1 or not account_activity_matches(state, "activate BCC " + row["local_dest"], matches[0].get("active"), 1):
            failures.append("BCC " + row["local_dest"])
    if failures:
        raise MigrationError("Inventory mismatch: " + ", ".join(failures))
    verify_recovery_emails(recovery, inventory["mailbox"])


def account_journal(batch: SavedPlan, configuration: Configuration) -> OperationJournal:
    identity = {"version": 1, "kind": "mailcow-account-staging", "hash": batch.fingerprint,
                "api": configuration.mailcow.api, "mailcow_directory": configuration.mailcow.directory}
    return OperationJournal(Workspace(batch.directory), "state.json", identity)


def require_before_activation(state: Mapping[str, Any]) -> None:
    if state.get("pending") or state.get("activated") or "enable domain for activation" in state["done"]:
        raise MigrationError("This operation is forbidden after target activation starts or during an uncertain target write")


def require_source_recovery(batch: SavedPlan, configuration: Configuration) -> None:
    journal = account_journal(batch, configuration)
    state = journal.read()
    require_before_activation(state)
    if "activationRequests" in state or (local_path(batch.directory / "target-isolation.json").exists()
                                        and not journal.workspace.path(journal.relative_path).exists()):
        raise MigrationError("Activation intent or missing account evidence requires reconciliation before source recovery")
    if local_path(batch.directory / "source-forwarding.json").exists() or local_path(batch.directory / "completed.json").exists():
        raise MigrationError("Source forwarding or completion has been recorded; reconcile handoff instead of releasing source controls")


def run_accounts(
    configuration: Configuration, file: Path, *, apply: bool = False, verify: bool = False, passwords: bool = False,
) -> int:
    if sys.platform != "linux" or os.geteuid() != 0:
        raise ConfigurationError("Run account staging on the Linux Mailcow host as root")
    if passwords and not verify:
        raise ConfigurationError("Use --passwords with verify")
    with Workspace().lock():
        batch = load_saved_plan(file, configuration)
        plan = batch.plan
        recovery, forwarding = recovery_plan(plan), mailbox_forwardings(plan)
        blockers = [row["detail"] for row in (*recovery["checks"], *forwarding["checks"]) if row["status"] == "blocked"]
        if blockers:
            raise MigrationError("Account mapping is blocked:\n" + "\n".join(blockers))
        journal = account_journal(batch, configuration)
        state = journal.read()
        policy_fingerprint = account_security_policy(batch, state)
        if not verify and ("activationRequests" in state or state.get("activated") or "enable domain for activation" in state["done"]):
            raise MigrationError("Account staging is forbidden after activation starts")
        operations = account_mutations(plan, recovery["emails"], reuse_domain=bool(state.get("existingDomain")))
        target = MailcowTarget(configuration.mailcow)
        if not verify:
            check_password_support(plan, target)
        api = target.api
        inventory = account_inventory(api) if "activationRequests" in state else None
        activation = account_activation_mutations(plan, inventory) if inventory is not None else ()
        validate_account_journal(state, operations, activation)
        pending = bool(state.get("pending"))
        reconcile_pending(journal, api, lambda kind, payload, password: verify_target_password(
            target, kind, payload, password, batch.directory,
        ))
        state = journal.read()
        print("Loading target domain, mailbox, alias-domain, alias and BCC inventories...")
        inventory = account_inventory(api) if inventory is None or pending else inventory
        if verify:
            if not state.get("accounts"):
                raise MigrationError("Complete account staging for this plan before verification")
            verify_account_inventory(plan, inventory, recovery["emails"], state)
            if passwords:
                verify_mailbox_passwords(plan, target, batch.directory)
                print("Stored mailbox hashes match the plan and Dovecot supports their schemes; no passwords were changed.")
            print("Domain, mailbox, alias, BCC and recovery inventories match the saved plan and recorded activation phase.")
            if activation and not state.get("activated"):
                print("Activation remains incomplete; resume activate with the original plan and journals.")
            print("Password login, messages, filters, sender permissions and delivery remain outside this account verification.")
            return 0
        if native_route_conflicts(plan, inventory["alias"]):
            raise MigrationError("Target mailbox addresses have separate alias routes; reconcile them before continuing")
        existing = None
        if not state["done"]:
            assessment = account_conflicts(plan, inventory)
            if state.get("existingDomain") and not any(
                (row.get("domain_name") or row.get("domain")) == plan["domain"] for row in inventory["domain"]
            ):
                raise MigrationError("Previously selected empty domain disappeared; reconcile the journal before resuming")
            if assessment["domainAction"] == "conflict":
                write_private_json(batch.directory / "target-conflict.json", {
                    "api": configuration.mailcow.api, "domain": plan["domain"], **assessment["conflicts"],
                }, replace=True)
                raise MigrationError("Existing target objects conflict with this batch; "
                                     "review target-conflict.json and retain any original plan/journal")
            existing = assessment["existingDomain"]
            if existing is not None:
                print("Reuse existing empty domain {} after applying these settings:".format(plan["domain"]))
                for change in assessment["domainChanges"]:
                    print("  {}: {} -> {}".format(change["field"], change["before"], change["after"]))
                print("The domain is enabled for account creation, then disabled. Its original settings are saved before the first edit.")
        operations = account_mutations(plan, recovery["emails"], reuse_domain=existing is not None or bool(state.get("existingDomain")))
        validate_account_journal(state, operations)
        findings = batch.blocking_findings
        if findings:
            message = "Resolve the saved planning findings before account writes:\n" + findings
            if apply:
                raise MigrationError(message)
            print(message)
        if apply:
            require_server_settings(batch, configuration)
            state = {**journal.read(), "policy_sha256": policy_fingerprint}
            if existing is not None and not state.get("existingDomain"):
                state = {**state, "existingDomain": existing}
            journal.save(state)
        for index, operation in enumerate(operations):
            if account_security_policy(batch, journal.read()) != policy_fingerprint:
                raise MigrationError("Settings policy changed during account staging; retain the original policy")
            outcome = journal.write_mutation(operation.identifier, operation.endpoint, operation.payload, api.request, apply=apply)
            print("{}/{}: {}: {}".format(index + 1, len(operations), outcome, operation.identifier))
            if apply and recovery["emails"] and operation.identifier == recovery["emails"][-1]["operation"]:
                verify_recovery_emails(recovery["emails"], api.inventory("mailbox"))
        if apply:
            verify_mailbox_passwords(plan, target, batch.directory)
            account_security_policy(batch, journal.read())
            journal.save({**journal.read(), "accounts": True})
            print("Accounts staged inactive. Keep the plan and journal together; copying, isolation and activation remain separate steps.")
        else:
            print("Account preview finished; no target accounts were changed.")
    return 0
