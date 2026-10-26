from typing import Any, Mapping, Sequence

from .reconciliation import api_number, api_string
from ..core.errors import MigrationError

SECURITY_FIELDS = ("force_tfa", "force_pw_update")


def sender_mailbox(username: str, mailboxes: Sequence[Mapping[str, Any]], *, inactive: bool = False) -> Mapping[str, Any]:
    matches = [row for row in mailboxes if row.get("username") == username]
    if len(matches) != 1 or not isinstance(matches[0].get("sender_acl"), list) or any(
            not isinstance(value, str) for value in matches[0]["sender_acl"]):
        raise MigrationError(username + ": target must expose one mailbox with a valid sender_acl")
    if inactive and api_number(matches[0].get("active")) != 0:
        raise MigrationError(username + ": mailbox must remain staged inactive")
    return matches[0]


def sender_allowed(mailbox: Mapping[str, Any], aliases: Sequence[Mapping[str, Any]], sender: str) -> bool:
    return (sender == mailbox["username"]
            or any(grant in mailbox["sender_acl"] for grant in (sender, "*", "@" + sender.rsplit("@", 1)[-1]))
            or any(row.get("address") == sender and row.get("sender_allowed") in (True, 1, "1")
                   and mailbox["username"] in [item.strip() for item in api_string(row.get("goto")).split(",")] for row in aliases))


def security_flags(mailbox: Mapping[str, Any]) -> Mapping[str, Any]:
    attributes = mailbox.get("attributes")
    if not isinstance(attributes, dict):
        raise MigrationError(mailbox["username"] + ": cannot preserve mailbox security attributes")
    flags = {}
    for key in SECURITY_FIELDS:
        value = attributes.get(key)
        if value is not None and type(value) not in (str, bool, int, float):
            raise MigrationError(mailbox["username"] + ": unrecognized security attribute " + key)
        number = api_number(value or 0)
        if number is None:
            raise MigrationError(mailbox["username"] + ": unrecognized security attribute " + key)
        flags[key] = int(number) if number.is_integer() else number
    return flags


def require_sender_permissions(request: Mapping[str, Any], mailbox: Mapping[str, Any], aliases: Sequence[Mapping[str, Any]]) -> None:
    if (not all(sender_allowed(mailbox, aliases, sender) for sender in request["senders"])
            or not set(request["preserved"]) <= set(mailbox["sender_acl"])):
        raise MigrationError(request["username"] + ": requested or preserved sender permissions differ; no overwrite performed")


def verify_sender_requests(requests: Sequence[Mapping[str, Any]], mailboxes: Sequence[Mapping[str, Any]], aliases: list) -> None:
    for row in requests:
        require_sender_permissions(row, sender_mailbox(row["username"], mailboxes), aliases)
