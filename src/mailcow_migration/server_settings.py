import re
import secrets
from pathlib import Path
from typing import Any, Mapping, Sequence

from .configuration import Configuration
from .core.errors import ConfigurationError, MigrationError
from .core.state import (
    OperationJournal,
    Workspace,
    reconcile_observed_mutation,
    require_identity,
)
from .core.storage import (
    json_fingerprint,
    private_directory,
    read_json,
    write_private_json,
)
from .plans.global_settings import plan_global_settings
from .plans.postfix import postfix_evidence
from .plans.server_state import server_directory, server_identity, verify_server_receipt
from .server_export import load_server_export
from .sources.sql import parse_sql
from .targets.global_api import inspect_transport
from .targets.global_postfix import GlobalPostfix
from .targets.mailcow import MailcowTarget
from .targets.passwords import verify_target_password
from .targets.reconciliation import api_number
from .targets.relay_credentials import relay_credentials
from .transfers import require_mailcow_host


def server_selection(
    file: Path,
    identity: Mapping[str, Any],
    inventory: Sequence[Mapping[str, Any]],
    *,
    review: bool,
) -> Mapping[str, Any]:
    saved = (
        read_json(file, private=True)
        if file.exists()
        else {**identity, "reviewed": False, "decisions": {}}
    )
    if (
        not isinstance(saved, dict)
        or type(saved.get("version")) is not int
        or type(saved.get("reviewed")) is not bool
        or not isinstance(saved.get("decisions"), dict)
        or any(not isinstance(value, dict) for value in saved["decisions"].values())
    ):
        raise MigrationError("Invalid server-settings selection; retain " + str(file))
    require_identity(saved, identity)
    if set(saved["decisions"]) - {row["id"] for row in inventory}:
        raise MigrationError(
            "Selections refer to settings outside this source inventory"
        )
    if review:
        print(
            "Review shared changes affecting all hosted domains. Exclusions preserve current target behavior."
        )
        for row in inventory:
            if row["status"] == "blocker":
                raise MigrationError(
                    "Required source setting has no supported mapping; inspect the private inventory before proceeding"
                )
            if row["status"] == "adapted":
                decision = {"action": "acknowledge"}
            else:
                admin = row["map"] == "global-administrators"
                choices = (
                    ("manage", "exclude")
                    if admin
                    else ("import", "exclude")
                    if row["status"] == "automatic"
                    else ("exclude",)
                )
                previous = saved["decisions"].get(row["id"], {}).get("action")
                print(
                    "{}: {} [{}]. Details remain in inventory.json.".format(
                        row["map"], row["key"], row["status"]
                    )
                )
                if row.get("kind") == "transport":
                    print(
                        "Recipient destination {} routes through {}. Review the default outgoing relay separately.".format(
                            row["destination"],
                            row["nexthop"],
                        )
                    )
                elif row.get("kind") == "relayhost":
                    print(
                        "Default outgoing relay: {}. Exclusion retains the target's existing default relay.".format(
                            row["nexthop"]
                        )
                    )
                elif row.get("kind") == "sender":
                    print(
                        "Reject this exact envelope sender for external unauthenticated SMTP; retain trusted/authenticated exemptions."
                    )
                elif row["status"] == "unsupported" and not admin:
                    print(
                        "No automatic mapping. Exclusion explicitly accepts the behavior difference; q leaves the review unfinished."
                    )
                if row.get("adaptation"):
                    print(
                        "This single-IP exception bypasses Postscreen, including RBL checks; Rspamd and relay restrictions remain."
                    )
                answer = input(
                    "Choose {}{}; q stops: ".format(
                        "/".join(choices),
                        " [" + previous + "]" if previous in choices else "",
                    )
                ).strip()
                answer = answer or (previous if previous in choices else "q")
                if answer == "q":
                    return saved
                if answer not in choices:
                    raise ConfigurationError(
                        "Use one of the displayed server-setting choices"
                    )
                decision = {"action": answer}
                if answer == "manage":
                    proposed = (
                        saved["decisions"].get(row["id"], {}).get("username")
                        or row["key"]
                    )
                    username = (
                        input(f"Target FULL SERVER administrator [{proposed}]: ")
                        .strip()
                        .lower()
                        or proposed
                    )
                    if not re.fullmatch(r"[a-z0-9_.-]+", username) or username == "api":
                        raise ConfigurationError(
                            "Use a valid target administrator login"
                        )
                    if (
                        input(
                            "Type approve to confirm identity, existing-account ownership and full server privileges: "
                        ).strip()
                        != "approve"
                    ):
                        return saved
                    decision = {
                        "action": "manage",
                        "username": username,
                        "approved": True,
                    }
            saved = {
                **saved,
                "reviewed": False,
                "decisions": {**saved["decisions"], row["id"]: decision},
            }
            write_private_json(file, saved, replace=True)
        saved = {**saved, "reviewed": True}
        write_private_json(file, saved, replace=True)
    elif not file.exists():
        write_private_json(file, saved)
    return saved


def selected_server_rows(
    inventory: Sequence[Mapping[str, Any]], selection: Mapping[str, Any]
) -> tuple:
    if selection.get("reviewed") is not True:
        raise MigrationError(
            "Complete server-settings --action review before applying or verifying shared changes"
        )
    selected, excluded, administrators = [], [], []
    for row in inventory:
        decision = selection["decisions"].get(row["id"], {})
        action = decision.get("action")
        if row["status"] == "blocker":
            raise MigrationError(
                "A required source setting remains blocked; it cannot be excluded"
            )
        if row["status"] == "adapted" and action == "acknowledge":
            continue
        if (
            action == "exclude"
            and row["status"] in ("automatic", "unsupported")
            and not row.get("missing")
        ):
            excluded.append(row["id"])
        elif (
            row["map"] == "global-administrators"
            and action == "manage"
            and decision.get("approved") is True
        ):
            username = decision.get("username")
            if (
                not isinstance(username, str)
                or not re.fullmatch(r"[a-z0-9_.-]+", username)
                or username == "api"
            ):
                raise MigrationError("Invalid reviewed server administrator name")
            administrators.append({**row, "username": username})
        elif row["status"] == "automatic" and action == "import":
            selected.append(row)
        else:
            raise MigrationError(
                "Every shared setting requires a valid explicit decision: "
                + row["map"]
                + ":"
                + row["key"]
            )
    if len({row["username"] for row in administrators}) != len(administrators):
        raise MigrationError("Server administrator mappings collide")
    return tuple(selected), tuple(excluded), tuple(administrators)


def run_server_settings(
    configuration: Configuration,
    exported: Path,
    *,
    action: str = "preview",
    apply: bool = False,
) -> int:
    require_mailcow_host()
    if action not in ("preview", "review", "apply", "check") or apply != (
        action == "apply"
    ):
        raise ConfigurationError(
            "Use --action apply --apply after reviewing shared settings; preview/review/check do not change target services"
        )
    with Workspace().lock():
        evidence = load_server_export(configuration, exported)
        parsed = parse_sql(evidence.data / "source-settings.sql")
        if not {"username", "active", "superadmin"} <= set(
            parsed["schemas"].get("admin", ())
        ):
            raise MigrationError(
                "Shared source export lacks the required administrator schema"
            )
        source_admins = parsed["rows"]["admin"]
        if any(
            not isinstance(row.get("username"), str)
            or not row["username"]
            or re.search(r"[\x00-\x1f\x7f]", row["username"])
            or api_number(row.get("active")) not in (0, 1)
            or api_number(row.get("superadmin")) not in (0, 1)
            for row in source_admins
        ):
            raise MigrationError(
                "Shared source administrator identities or activity flags are invalid"
            )
        postfix = postfix_evidence(evidence, source_admins)
        inventory = plan_global_settings(postfix)
        fingerprint = json_fingerprint(inventory)
        directory = private_directory(server_directory(evidence.source, configuration))
        identity = server_identity(evidence.source, configuration)
        if not (directory / "state.json").exists() and any(
            (directory / name).exists()
            for name in ("postfix-state.json", "admin-credentials.json")
        ):
            raise MigrationError(
                "Shared operation evidence exists without its state; restore the original journal before proceeding"
            )
        previous = (
            read_json(directory / "state.json", private=True)
            if (directory / "state.json").exists()
            else {}
        )
        if not isinstance(previous, dict):
            raise MigrationError("Invalid shared-setting journal")
        locked = "actions" in previous
        if locked:
            require_identity(
                previous, {**identity, "inventory_fingerprint": fingerprint}
            )
            if not (directory / "selection.json").exists():
                raise MigrationError(
                    "Restore the original shared-setting selection; its operations already have a journal"
                )
        write_private_json(
            directory / "inventory.json",
            {
                **identity,
                "inventory_fingerprint": fingerprint,
                "inventory": inventory,
                "export": str(evidence.directory),
            },
            replace=True,
        )
        selection_identity = {
            **identity,
            "kind": "server-settings-selection",
            "inventory_fingerprint": fingerprint,
        }
        selection = server_selection(
            directory / "selection.json",
            selection_identity,
            inventory,
            review=action == "review" and not locked,
        )
        if locked and previous.get("selection_fingerprint") != json_fingerprint(
            selection, canonical=True
        ):
            raise MigrationError(
                "Shared choices changed after operations started; restore the original selection"
            )
        print(f"Shared inventory and review: {directory}. {len(inventory)} settings.")
        if not selection["reviewed"]:
            print(
                "Review is incomplete. Run server-settings --action review with this export."
            )
            return 1
        if action == "review":
            print(
                "Choices retained for the recorded operations."
                if locked
                else "Choices saved. Review the shared scope, then use --action apply --apply."
            )
            return 0
        selected, excluded, managed = selected_server_rows(inventory, selection)
        credentials = (
            relay_credentials(evidence.data, postfix.postfix or "")
            if any(row.get("nexthop") for row in selected)
            else {}
        )
        actions = tuple(
            {
                **row,
                "credentials": credentials.get(
                    row.get("nexthop"), {"username": "", "password": ""}
                ),
            }
            for row in selected
        )
        actions = tuple(
            {
                **row,
                "payload": {
                    "destination": row["destination"],
                    "nexthop": row["nexthop"],
                    "active": 1,
                    "is_mx_based": 0,
                    **row["credentials"],
                },
            }
            if row["kind"] == "transport"
            else row
            for row in actions
        )
        administrators = []
        for row in managed:
            matches = tuple(
                item
                for item in source_admins
                if item["username"] == row["key"]
                and api_number(item["superadmin"]) == 1
            )
            if len(matches) != 1 or api_number(matches[0]["active"]) not in (0, 1):
                raise MigrationError(
                    "Source server administrator identity/activity is ambiguous"
                )
            administrators.append(
                {
                    "id": row["id"],
                    "username": row["username"],
                    "active": int(api_number(matches[0]["active"])),
                }
            )
        binding = {
            **identity,
            "inventory_fingerprint": fingerprint,
            "selection_fingerprint": json_fingerprint(selection, canonical=True),
            "actions_fingerprint": json_fingerprint(actions, canonical=True),
        }
        journal = OperationJournal(Workspace(directory), "state.json", binding)
        state = journal.read()
        if action == "check":
            verify_server_receipt(evidence.source, configuration, fingerprint)
            print(
                "Selected shared settings and server administrator identities verified; exclusions remain recorded."
            )
            return 0
        target = MailcowTarget(configuration.mailcow)
        routes = (
            target.api.inventory("transport")
            if any(row["kind"] == "transport" for row in actions)
            else []
        )
        domains = (
            target.api.inventory("domain")
            if routes or any(row["kind"] == "transport" for row in actions)
            else []
        )
        admin_inventory = target.api.inventory("admin") if administrators else []
        domain_admins = target.api.inventory("domain-admin") if administrators else []
        password_file = directory / "admin-credentials.json"
        passwords = (
            read_json(password_file, private=True) if password_file.exists() else {}
        )
        if (
            not isinstance(passwords, dict)
            or set(passwords) - {row["username"] for row in administrators}
            or any(
                not isinstance(value, str) or not value for value in passwords.values()
            )
        ):
            raise MigrationError(
                "Invalid shared administrator credential file; contents omitted"
            )
        for row in actions:
            if row["kind"] != "transport":
                continue
            exists = inspect_transport(row["payload"], routes, domains)
            identifier = "transport " + row["id"]
            if identifier in state["done"] and not exists:
                raise MigrationError(
                    "Previously imported shared transport disappeared; no replacement attempted"
                )
            reconcile_observed_mutation(
                journal, identifier, "add/transport", row["payload"], exists
            )
        for row in administrators:
            matches = tuple(
                item
                for item in admin_inventory
                if item.get("username") == row["username"]
            )
            if (
                len(matches) > 1
                or matches
                and api_number(matches[0].get("active")) != row["active"]
                or any(
                    item.get("username") == row["username"] for item in domain_admins
                )
            ):
                raise MigrationError(
                    "Target administrator conflicts with the reviewed full-server identity: "
                    + row["username"]
                )
            identifier = "administrator " + row["id"]
            if identifier in state["done"] and not matches:
                raise MigrationError(
                    "Previously verified server administrator disappeared"
                )
            if journal.read().get("pending") == identifier:
                password = passwords.get(row["username"])
                if password is None:
                    raise MigrationError(
                        "Restore the original private administrator credentials before reconciling this creation"
                    )
                payload = {
                    "username": row["username"],
                    "active": row["active"],
                    "password": password,
                    "password2": password,
                }
                verified = bool(matches) and verify_target_password(
                    target, "admin", payload, password, directory
                )
                reconcile_observed_mutation(
                    journal, identifier, "add/admin", payload, verified
                )
        if journal.read().get("pending"):
            raise MigrationError(
                "Shared journal has an unresolved operation outside the current selection"
            )
        config_rows = tuple(row for row in actions if row["kind"] != "transport")
        postfix_update = (
            GlobalPostfix(target, config_rows, directory) if config_rows else None
        )
        evidence.verify()
        if not apply:
            print(
                f"{len(actions)} routes/configuration mappings; "
                f"{len(administrators)} approved full-server administrators; {len(excluded)} exclusions."
            )
            print(
                "Target preflight completed. Shared Postfix changes may reload Postfix for all domains; no target writes performed."
            )
            return 0
        journal.save(
            {
                **journal.read(),
                "complete": False,
                "actions": actions,
                "administrators": administrators,
                "excluded": excluded,
                "export": str(evidence.directory),
            }
        )
        for row in administrators:
            if (
                not any(
                    item.get("username") == row["username"] for item in admin_inventory
                )
                and row["username"] not in passwords
            ):
                passwords = {
                    **passwords,
                    row["username"]: "Aa1!" + secrets.token_urlsafe(24),
                }
        if passwords:
            write_private_json(password_file, passwords, replace=True)
        for row in actions:
            if row["kind"] != "transport":
                continue
            exists = inspect_transport(
                row["payload"],
                target.api.inventory("transport"),
                target.api.inventory("domain"),
            )
            identifier = "transport " + row["id"]
            if not exists:
                journal.write_mutation(
                    identifier,
                    "add/transport",
                    row["payload"],
                    target.api.request,
                    apply=True,
                )
            if not inspect_transport(
                row["payload"],
                target.api.inventory("transport"),
                target.api.inventory("domain"),
            ):
                raise MigrationError("Imported shared transport failed readback")
            journal.save(
                {
                    **journal.read(),
                    "done": list(dict.fromkeys([*journal.read()["done"], identifier])),
                }
            )
        if postfix_update is not None:
            postfix_update.apply()
        for row in administrators:
            found = tuple(
                item
                for item in target.api.inventory("admin")
                if item.get("username") == row["username"]
            )
            if not found:
                password = passwords[row["username"]]
                payload = {
                    "username": row["username"],
                    "active": row["active"],
                    "password": password,
                    "password2": password,
                }
                journal.write_mutation(
                    "administrator " + row["id"],
                    "add/admin",
                    payload,
                    target.api.request,
                    apply=True,
                )
            if (
                row["id"] not in journal.read().get("verified_administrators", [])
                and row["username"] in passwords
                and not verify_target_password(
                    target,
                    "admin",
                    {"username": row["username"]},
                    passwords[row["username"]],
                    directory,
                )
            ):
                raise MigrationError(
                    "New server administrator password did not verify; no password reset attempted"
                )
            actual = tuple(
                item
                for item in target.api.inventory("admin")
                if item.get("username") == row["username"]
            )
            if len(actual) != 1 or api_number(actual[0].get("active")) != row["active"]:
                raise MigrationError(
                    "Server administrator readback failed: " + row["username"]
                )
            journal.save(
                {
                    **journal.read(),
                    "done": list(
                        dict.fromkeys(
                            [*journal.read()["done"], "administrator " + row["id"]]
                        )
                    ),
                    "verified_administrators": list(
                        dict.fromkeys(
                            [
                                *journal.read().get("verified_administrators", []),
                                row["id"],
                            ]
                        )
                    ),
                }
            )
        evidence.verify()
        verify_server_receipt(
            evidence.source, configuration, fingerprint, require_complete=False
        )
        journal.save({**journal.read(), "complete": True})
        print(
            "Shared settings applied and verified. Exclusions and any new administrator credentials are retained privately in "
            + str(directory)
        )
    return 0
