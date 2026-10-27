from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

from ..configuration import Configuration
from ..core.errors import MigrationError
from ..core.state import Workspace, require_identity
from ..core.storage import json_fingerprint, read_json
from ..sources import SourceConnection, SourceKind
from ..targets.global_api import inspect_transport
from ..targets.global_postfix import GlobalPostfix
from ..targets.mailcow import MailcowTarget
from ..targets.reconciliation import api_number
from .saved import SavedPlan


def server_directory(source: SourceConnection, configuration: Configuration) -> Path:
    return Workspace().path(
        "global/"
        + json_fingerprint(
            {
                "source": source.ssh.host,
                "api": configuration.mailcow.api,
                "mailcow_directory": configuration.mailcow.directory,
            },
            canonical=True,
        )
    )


def server_identity(
    source: SourceConnection, configuration: Configuration
) -> Mapping[str, Any]:
    return {
        "version": 1,
        "kind": "mailcow-server-settings",
        "source": asdict(source),
        "api": configuration.mailcow.api,
        "mailcow_directory": configuration.mailcow.directory,
    }


def verify_server_receipt(
    source: SourceConnection,
    configuration: Configuration,
    fingerprint: str,
    *,
    require_complete: bool = True,
) -> Mapping[str, Any]:
    directory = server_directory(source, configuration)
    if not (directory / "state.json").exists():
        raise MigrationError(
            "Run export-server and server-settings review/application before changing PostfixAdmin domains"
        )
    state = read_json(directory / "state.json", private=True)
    if (
        not isinstance(state, dict)
        or type(state.get("version")) is not int
        or type(state.get("complete")) is not bool
        or require_complete
        and not state["complete"]
        or state.get("pending")
        or not isinstance(state.get("actions"), list)
        or not isinstance(state.get("administrators"), list)
    ):
        raise MigrationError(
            "Complete server-settings review/application before domain migration; retain shared recovery evidence"
        )
    require_identity(
        state,
        {
            **server_identity(source, configuration),
            "inventory_fingerprint": fingerprint,
        },
    )
    if (
        any(
            not isinstance(row, dict)
            or not isinstance(row.get("id"), str)
            or row.get("kind")
            not in ("transport", "relayhost", "sender", "postscreen-whitelist")
            for row in state["actions"]
        )
        or any(
            not isinstance(row, dict)
            or not isinstance(row.get("id"), str)
            or not isinstance(row.get("username"), str)
            or type(row.get("active")) is not int
            or row["active"] not in (0, 1)
            for row in state["administrators"]
        )
        or not isinstance(state.get("verified_administrators", []), list)
    ):
        raise MigrationError("Invalid shared-setting or administrator receipt")
    for row in state["actions"]:
        credentials = row.get("credentials")
        if (
            not isinstance(credentials, dict)
            or set(credentials) != {"username", "password"}
            or any(not isinstance(value, str) for value in credentials.values())
            or not isinstance(row.get("key"), str)
        ):
            raise MigrationError(
                "Invalid shared action or credential reference; contents omitted"
            )
        if row["kind"] in ("transport", "relayhost") and not isinstance(
            row.get("nexthop"), str
        ):
            raise MigrationError("Invalid shared relay destination")
        if row["kind"] == "transport":
            payload = row.get("payload")
            if (
                not isinstance(payload, dict)
                or any(
                    not isinstance(payload.get(key), str)
                    for key in ("destination", "nexthop", "username", "password")
                )
                or not isinstance(row.get("destination"), str)
            ):
                raise MigrationError("Invalid shared transport receipt")
    if state.get("actions_fingerprint") != json_fingerprint(
        state["actions"], canonical=True
    ):
        raise MigrationError("Shared-setting actions changed after verification")
    selection = read_json(directory / "selection.json", private=True)
    if (
        not isinstance(selection, dict)
        or selection.get("reviewed") is not True
        or not isinstance(selection.get("decisions"), dict)
        or state.get("selection_fingerprint")
        != json_fingerprint(selection, canonical=True)
    ):
        raise MigrationError("Shared selection changed after verification")
    decisions = selection["decisions"]
    if any(not isinstance(decision, dict) for decision in decisions.values()):
        raise MigrationError("Invalid shared-setting decisions")
    if (
        {row["id"] for row in state["actions"]}
        != {key for key, value in decisions.items() if value.get("action") == "import"}
        or {row["id"] for row in state["administrators"]}
        != {key for key, value in decisions.items() if value.get("action") == "manage"}
        or any(
            row["id"] not in state.get("verified_administrators", [])
            for row in state["administrators"]
        )
    ):
        raise MigrationError(
            "Shared completion does not cover the reviewed imports and administrator identities"
        )
    target = MailcowTarget(configuration.mailcow)
    transports = tuple(
        row for row in state["actions"] if row.get("kind") == "transport"
    )
    routes = target.api.inventory("transport") if transports else ()
    domains = target.api.inventory("domain") if transports else ()
    for row in transports:
        if not inspect_transport(row["payload"], routes, domains):
            raise MigrationError(
                "Shared target transport no longer matches: " + row["destination"]
            )
    configuration_rows = tuple(
        row for row in state["actions"] if row.get("kind") != "transport"
    )
    if configuration_rows:
        GlobalPostfix(target, configuration_rows, directory).verify()
    administrators = target.api.inventory("admin") if state["administrators"] else ()
    for expected in state["administrators"]:
        matches = tuple(
            row for row in administrators if row.get("username") == expected["username"]
        )
        if (
            len(matches) != 1
            or api_number(matches[0].get("active")) != expected["active"]
        ):
            raise MigrationError(
                "Shared administrator no longer matches its approved identity: "
                + expected["username"]
            )
    return state


def require_server_settings(batch: SavedPlan, configuration: Configuration) -> None:
    if batch.source.kind is SourceKind.PLESK:
        return
    assessment = read_json(batch.directory / "shared-settings-plan.json", private=True)
    if not isinstance(assessment, dict) or not isinstance(
        assessment.get("inventory_fingerprint"), str
    ):
        raise MigrationError("Missing shared-server assessment")
    verify_server_receipt(
        batch.source, configuration, assessment["inventory_fingerprint"]
    )
