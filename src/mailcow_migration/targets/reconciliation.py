import math
from typing import Any, Callable, Mapping, NoReturn, Optional

from ..core.errors import MigrationError
from ..core.state import OperationJournal
from .account_inventory import DOMAIN_FIELDS
from .mailcow import MailcowApi

NUMERIC = frozenset(
    (
        "active",
        "quota",
        "aliases",
        "mailboxes",
        "defquota",
        "maxquota",
        "backupmx",
        "relay_all_recipients",
        "force_pw_update",
        "force_tfa",
        "goto_null",
    )
)


def api_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        converted = float((value.strip() if isinstance(value, str) else value) or 0)
    except (ValueError, TypeError):
        return None
    return converted if math.isfinite(converted) else None


def api_string(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def normalized(value: Any) -> str:
    return (
        ",".join(sorted(api_string(item) for item in value))
        if isinstance(value, list)
        else api_string(value)
    )


def alias_destinations(value: Any) -> str:
    return ",".join(sorted(item.strip() for item in api_string(value).split(",")))


def request_matches(
    row: Mapping[str, Any], verb: str, kind: str, payload: Mapping[str, Any]
) -> bool:
    if verb == "edit":
        key = (
            row.get("domain_name") or row.get("domain")
            if kind == "domain"
            else row.get("username")
            if kind in ("mailbox", "domain-admin")
            else row.get("alias_domain")
            if kind == "alias-domain"
            else row.get("id")
        )
        return key is not None and api_string(key) in {
            api_string(item) for item in payload["items"]
        }
    if kind == "domain":
        return (row.get("domain_name") or row.get("domain")) == payload.get("domain")
    if kind == "mailbox":
        return row.get("username") == "{}@{}".format(
            payload.get("local_part"), payload.get("domain")
        )
    if kind == "alias":
        return row.get("address") == payload.get("address")
    if kind == "alias-domain":
        return row.get("alias_domain") == payload.get("alias_domain")
    if kind == "domain-admin":
        return row.get("username") == payload.get("username")
    if kind == "bcc":
        return all(
            row.get(key) == payload.get(key)
            for key in ("local_dest", "bcc_dest", "type")
        )
    if kind == "filter":
        return all(
            row.get(key) == payload.get(key)
            for key in ("username", "filter_type", "script_desc")
        )
    return False


def requested_values_match(
    row: Mapping[str, Any],
    kind: str,
    desired: Mapping[str, Any],
    password_matches: bool,
) -> list:
    mismatches = []
    attributes = (
        row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
    )
    for key, expected in desired.items():
        actual = row.get(key)
        if kind == "domain":
            column = "domain_name" if key == "domain" else DOMAIN_FIELDS.get(key)
            if column and row.get(column) is not None:
                actual = row[column]
        if (
            key == "quota"
            and kind == "mailbox"
            or kind == "domain"
            and key in ("defquota", "maxquota", "quota")
        ):
            value = api_number(expected)
            expected = value * 1048576 if value is not None else None
        if kind == "mailbox":
            if key == "local_part":
                username = row.get("username")
                actual = username.split("@")[0] if isinstance(username, str) else None
            elif key == "pw_recovery_email":
                actual = attributes.get("recovery_email")
            elif key in ("quarantine_notification", "quarantine_category"):
                actual = attributes.get(key)
        if key in ("force_pw_update", "force_tfa") and attributes.get(key) is not None:
            actual = attributes[key]
        if kind == "domain-admin" and key == "domains":
            actual = row.get("selected_domains")
        if key in ("password", "password2"):
            if not password_matches or expected != desired.get("password"):
                mismatches.append(
                    key + " was not verified against the target password hash"
                )
            continue
        if key == "goto_null":
            actual = int(row.get("goto") == "null@localhost")
        elif key == "goto":
            actual, expected = (
                alias_destinations(row.get("goto")),
                alias_destinations(expected),
            )
        if actual is None or (
            api_number(actual) is None or api_number(actual) != api_number(expected)
            if key in NUMERIC
            else normalized(actual) != normalized(expected)
        ):
            mismatches.append(key)
    return mismatches


def reconcile_pending(
    journal: OperationJournal,
    api: MailcowApi,
    verify_password: Optional[Callable[[str, Mapping[str, Any], str], bool]] = None,
) -> None:
    state = journal.read()
    if not state.get("pending"):
        return

    def blocked(reason: str) -> NoReturn:
        raise MigrationError(
            "Uncertain operation '{}' in {}: {}. No API write was repeated. "
            "Retain the journal and its request for reconciliation.".format(
                state["pending"], journal.workspace.path(journal.relative_path), reason
            )
        )

    request = state.get("pendingRequest")
    if (
        not isinstance(request, dict)
        or not isinstance(request.get("endpoint"), str)
        or not isinstance(request.get("payload"), dict)
    ):
        blocked("the journal has no valid saved request to compare with Mailcow")
    pieces = request["endpoint"].split("/")
    if (
        len(pieces) != 2
        or pieces[0] not in ("add", "edit")
        or pieces[1]
        not in (
            "domain",
            "mailbox",
            "alias",
            "alias-domain",
            "domain-admin",
            "bcc",
            "filter",
        )
    ):
        blocked("unsupported operation")
    verb, kind = pieces
    payload = request["payload"]
    if verb == "edit" and (
        not isinstance(payload.get("items"), list)
        or not payload["items"]
        or not isinstance(payload.get("attr"), dict)
    ):
        blocked("invalid edit payload")
    desired = payload["attr"] if verb == "edit" else payload
    inventory = api.inventory("filters" if kind == "filter" else kind)
    found = [row for row in inventory if request_matches(row, verb, kind, payload)]
    expected_count = len(payload["items"]) if verb == "edit" else 1
    if len(found) != expected_count:
        blocked(
            f"expected {expected_count} matching target objects, found {len(found)}"
        )
    password_matches = bool(
        desired.get("password")
        and verify_password
        and verify_password(kind, payload, desired["password"])
    )
    mismatches = list(
        dict.fromkeys(
            item
            for row in found
            for item in requested_values_match(row, kind, desired, password_matches)
        )
    )
    if mismatches:
        blocked("target values differ or are not observable: " + ", ".join(mismatches))
    completed = {
        key: value
        for key, value in state.items()
        if key not in ("pending", "pendingRequest")
    }
    journal.save(
        {**completed, "done": list(dict.fromkeys([*state["done"], state["pending"]]))}
    )
    print("Confirmed on target; no repeated write: " + state["pending"])
