import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Tuple

from .configuration import Configuration
from .core.errors import ConfigurationError, MigrationError
from .core.state import OperationJournal, Workspace
from .core.storage import write_private_json
from .plans.domain import MIB
from .plans.forwarding import mailbox_forwardings
from .plans.recovery import recovery_plan
from .plans.saved import SavedPlan, load_saved_plan
from .targets.account_inventory import DOMAIN_FIELDS, account_conflicts, account_inventory, domain_value, native_route_conflicts
from .targets.mailcow import MailcowTarget
from .targets.passwords import verify_target_password
from .targets.reconciliation import alias_destinations, api_number, reconcile_pending


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


def validate_account_journal(state: Mapping[str, Any], operations: Tuple[AccountMutation, ...]) -> None:
    expected = {row.identifier: {"endpoint": row.endpoint, "payload": row.payload} for row in operations}
    if len(expected) != len(operations):
        raise MigrationError("Plan repeats an account operation identity; resolve the source mappings")
    if any(name not in expected for name in state["done"]):
        raise MigrationError("The journal contains operations outside account staging; retain it for the matching recovery workflow")
    if state.get("pending") and (state["pending"] not in expected or state.get("pendingRequest") != expected[state["pending"]]):
        raise MigrationError("The pending request differs from this account plan; no reconciliation or write was attempted")
    if "pendingRequest" in state and not state.get("pending"):
        raise MigrationError("The journal has a request without an operation identity; retain it for reconciliation")
    if state.get("accounts") and any(row.identifier not in state["done"] for row in operations):
        raise MigrationError("The account-completion marker is inconsistent with the saved operations")


def verify_recovery_emails(emails: list, mailboxes: list) -> None:
    for row in emails:
        mailbox = next((item for item in mailboxes if item.get("username") == row["username"]), {})
        attributes = mailbox.get("attributes") if isinstance(mailbox.get("attributes"), dict) else {}
        if attributes.get("recovery_email") != row["email"]:
            raise MigrationError("Recovery email mismatch for {}; verify target support for pw_recovery_email".format(row["username"]))


def verify_account_inventory(plan: Mapping[str, Any], inventory: Mapping[str, list], recovery: list, state: Mapping[str, Any]) -> None:
    activated = bool(state.get("activated"))
    failures = []
    primary = next((row for row in inventory["domain"] if (row.get("domain_name") or row.get("domain")) == plan["domain"]), {})
    if not primary or api_number(primary.get("active")) != (plan["domainActive"] if activated else 0):
        failures.append("domain " + plan["domain"])
    for key, column in DOMAIN_FIELDS.items():
        if primary.get(column) is None or api_number(domain_value(primary, key)) != plan["domainPayload"][key]:
            failures.append("domain {}: {}".format(plan["domain"], column))
    for row in plan["mailboxes"]:
        found = next((item for item in inventory["mailbox"] if item.get("username") == row["username"]), {})
        if (not found or found.get("name") != row["payload"]["name"] or api_number(found.get("active")) != (row["active"] if activated else 0)
                or api_number(found.get("quota")) != row["payload"]["quota"] * MIB):
            failures.append("mailbox " + row["username"])
        attributes = found.get("attributes") if isinstance(found.get("attributes"), dict) else {}
        for key in ("quarantine_notification", "quarantine_category"):
            if key in row["payload"] and attributes.get(key) != row["payload"][key]:
                failures.append("mailbox {}: {}".format(row["username"], key))
    for row in plan["aliasDomains"]:
        found = next((item for item in inventory["alias-domain"] if item.get("alias_domain") == row["alias_domain"]), {})
        if (not found or found.get("target_domain") != row["target_domain"]
                or api_number(found.get("active")) != (api_number(row["active"]) if activated else 0)):
            failures.append("alias domain " + row["alias_domain"])
    if native_route_conflicts(plan, inventory["alias"]):
        failures.append("separate aliases conflict with native mailbox delivery")
    for row in plan["aliases"]:
        if row["mailbox"]:
            continue
        found = next((item for item in inventory["alias"] if item.get("address") == row["address"]), {})
        destination = "null@localhost" if row["payload"].get("goto_null") else row["payload"].get("goto")
        if (not found or alias_destinations(found.get("goto")) != alias_destinations(destination)
                or api_number(found.get("active")) != (row["active"] if activated else 0)):
            failures.append("alias " + row["address"])
    for row in plan["bcc"]:
        matches = [item for item in inventory["bcc"] if all(item.get(key) == row[key] for key in ("local_dest", "bcc_dest", "type"))]
        if len(matches) != 1 or api_number(matches[0].get("active")) != (1 if activated else 0):
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


def run_accounts(configuration: Configuration, file: Path, *, apply: bool = False, verify: bool = False) -> int:
    if sys.platform != "linux" or os.geteuid() != 0:
        raise ConfigurationError("Run account staging on the Linux Mailcow host as root")
    with Workspace().lock():
        batch = load_saved_plan(file, configuration)
        plan = batch.plan
        recovery, forwarding = recovery_plan(plan), mailbox_forwardings(plan)
        blockers = [row["detail"] for row in (*recovery["checks"], *forwarding["checks"]) if row["status"] == "blocked"]
        if blockers:
            raise MigrationError("Account mapping is blocked:\n" + "\n".join(blockers))
        journal = account_journal(batch, configuration)
        state = journal.read()
        if not verify and (state.get("activated") or "enable domain for activation" in state["done"]):
            raise MigrationError("Account staging is forbidden after activation starts")
        operations = account_mutations(plan, recovery["emails"], reuse_domain=bool(state.get("existingDomain")))
        validate_account_journal(state, operations)
        target = MailcowTarget(configuration.mailcow)
        api = target.api
        reconcile_pending(journal, api, lambda kind, payload, password: verify_target_password(
            target, kind, payload, password, batch.directory,
        ))
        state = journal.read()
        print("Loading target domain, mailbox, alias-domain, alias and BCC inventories...")
        inventory = account_inventory(api)
        if verify:
            if not state.get("accounts"):
                raise MigrationError("Complete account staging for this plan before verification")
            verify_account_inventory(plan, inventory, recovery["emails"], state)
            print("Domain, mailbox, alias, BCC and recovery inventories match the saved plan.")
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
            if existing is not None and not state.get("existingDomain"):
                journal.save({**state, "existingDomain": existing})
        for index, operation in enumerate(operations):
            outcome = journal.write_mutation(operation.identifier, operation.endpoint, operation.payload, api.request, apply=apply)
            print("{}/{}: {}: {}".format(index + 1, len(operations), outcome, operation.identifier))
            if apply and recovery["emails"] and operation.identifier == recovery["emails"][-1]["operation"]:
                verify_recovery_emails(recovery["emails"], api.inventory("mailbox"))
        if apply:
            journal.save({**journal.read(), "accounts": True})
            print("Accounts staged inactive. Keep the plan and journal together; copying, isolation and activation remain separate steps.")
        else:
            print("Account preview finished; no target accounts were changed.")
    return 0
