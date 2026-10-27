import json
import re
from typing import Any, Callable, Mapping, Sequence

from ..core.errors import MigrationError
from .assessment import check


def mailbox_delivery(row: Mapping[str, Any]) -> Mapping[str, Any]:
    targets = list(
        dict.fromkeys(
            item.strip()
            for item in str(row["payload"].get("goto") or "").split(",")
            if item.strip()
        )
    )
    return {
        "keepLocal": not row["payload"].get("goto_null") and row["address"] in targets,
        "destinations": [item for item in targets if item != row["address"]],
    }


def mailbox_forwardings(plan: Mapping[str, Any]) -> Mapping[str, Any]:
    checks, forwardings = [], []
    for row in plan["aliases"]:
        if not row["mailbox"]:
            continue
        mailbox = next(
            (item for item in plan["mailboxes"] if item["username"] == row["address"]),
            None,
        )
        delivery = mailbox_delivery(row)
        if (
            mailbox is None
            or not delivery["keepLocal"]
            or row["active"] != mailbox["active"]
        ):
            checks.append(
                check(
                    "mailbox-forwarding:" + row["address"],
                    "blocked",
                    "Automatic mailbox forwarding requires a local copy and matching mailbox/alias activity; source rule retained.",
                )
            )
        elif delivery["destinations"]:
            forwardings.append(
                {"username": row["address"], "active": row["active"], **delivery}
            )
    return {"checks": checks, "forwardings": forwardings}


def sieve_string(value: str) -> str:
    if not isinstance(value, str) or re.search(r"[\x00-\x1f\x7f]", value):
        raise MigrationError("Unsupported Sieve header text")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def json_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def mailbox_filter_plans(
    vacations: Sequence[Mapping[str, Any]],
    forwardings: Sequence[Mapping[str, Any]],
    quote: Callable[[str], str] = sieve_string,
) -> list:
    users = dict.fromkeys(row["username"] for row in (*vacations, *forwardings))
    plans = []
    for username in users:
        vacation = next((row for row in vacations if row["username"] == username), None)
        forwarding = next(
            (row for row in forwardings if row["username"] == username), None
        )
        if forwarding is None:
            plans.append(vacation)
        else:
            plans.append(
                {
                    "username": username,
                    "filter_type": "prefilter",
                    "active": 0,
                    "script_desc": "Migration forwarding{}: {}".format(
                        " and vacation" if vacation else "", username
                    ),
                    "script_data": 'require ["copy"];\n'
                    + (vacation["script_data"] if vacation else "")
                    + "".join(
                        f"redirect :copy {quote(target)};\n"
                        for target in forwarding["destinations"]
                    ),
                }
            )
    return plans
