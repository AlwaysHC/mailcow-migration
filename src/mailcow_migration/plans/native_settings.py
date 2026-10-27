import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from .domain import active
from .forwarding import json_string, mailbox_delivery, mailbox_filter_plans
from .sieve import SieveMappingError, requires_forwarding_prefilter, sieve_filters
from .sieve_evidence import SieveEvidence

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def vacation_timestamp(value: Any) -> Optional[int]:
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}", value
    ):
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None
    return int((parsed - EPOCH).total_seconds())


def utc_date(seconds: int) -> str:
    return (
        (EPOCH + timedelta(seconds=seconds))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def vacation_days(row: Mapping[str, Any], policy: Mapping[str, Any]) -> Any:
    if "interval_time" not in row:
        return None
    try:
        interval = row["interval_time"]
        seconds = float(
            (interval.strip() if isinstance(interval, str) else interval) or 0
        )
    except (TypeError, ValueError):
        return None
    days = policy.get("vacationZeroIntervalDays") if seconds == 0 else seconds / 86400
    if type(days) not in (int, float) or not math.isfinite(days):
        return None
    return int(days) if int(days) == days else days


def valid_vacation_days(value: Any) -> bool:
    return type(value) is int and 1 <= value <= 30


def mailbox_sieve(username: str, evidence: SieveEvidence) -> Mapping[str, Any]:
    if not re.fullmatch(
        r"[a-z0-9_.+%-]+@[a-z0-9.-]+", username, re.IGNORECASE | re.ASCII
    ):
        raise SieveMappingError("Unsupported Sieve username: " + username)
    script = evidence.script(username)
    if not isinstance(script, str):
        raise SieveMappingError("Missing personal/default Sieve evidence")
    return {
        "username": username,
        "script": script,
        "filters": sieve_filters(script),
        "preferences": {},
    }


def native_mailbox_settings(
    plan: Mapping[str, Any],
    evidence: SieveEvidence,
    policy: Mapping[str, Any],
    date_parts: Mapping[int, Mapping[str, str]],
    converted: Mapping[str, Mapping[str, Any]],
    filter_errors: Mapping[str, str],
) -> Mapping[str, Any]:
    users, blockers = [], []
    vacations = plan.get("source", {}).get("vacations")
    if not isinstance(vacations, list):
        blockers.append("Source vacation inventory is missing; refresh the plan.")
        vacations = []
    for row in vacations:
        if not any(
            mailbox["username"] == row.get("email") for mailbox in plan["mailboxes"]
        ):
            blockers.append(
                "{}: vacation has no target mailbox.".format(row.get("email"))
            )
    for mailbox in plan["mailboxes"]:
        username, preferences, prefilter = mailbox["username"], {}, None
        vacation = next(
            (row for row in vacations if row.get("email") == username), None
        )
        forwarding = next(
            (
                row
                for row in plan["aliases"]
                if row["mailbox"] and row["address"] == username
            ),
            None,
        )
        issues = []
        if vacation is not None:
            start, end = (
                vacation_timestamp(vacation.get("activefrom")),
                vacation_timestamp(vacation.get("activeuntil")),
            )
            days = vacation_days(vacation, policy)
            if not (evidence.dates_utc or policy.get("vacationDatesAreUtc")):
                issues.append("Vacation dates need a confirmed UTC source timezone.")
            if not valid_vacation_days(days):
                issues.append(
                    "SOGo cannot preserve this vacation reply interval; once-per-sender needs an explicitly approved repeat interval."
                )
            if (
                start is None
                or end is None
                or start >= end
                or start < 0
                or end > 2147483647
            ):
                issues.append(
                    "Vacation date range has no valid native SOGo representation."
                )
            elif tuple(
                date_parts[start][key] for key in ("hour", "minute", "second")
            ) != ("00", "00", "00") or tuple(
                date_parts[end][key] for key in ("hour", "minute", "second")
            ) != ("23", "59", "59"):
                issues.append(
                    "SOGo vacation dates cover whole days. "
                    "This source has partial-day boundaries; migration would change the reply window."
                )
            body, subject = vacation.get("body"), vacation.get("subject")
            if (
                not isinstance(body, str)
                or re.search(r"(^|[\r\n])\.|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", body)
                or not isinstance(subject, str)
                or re.search(r"[\x00-\x1f\x7f]|\$\{", subject)
            ):
                issues.append("Vacation text/subject needs a safe native SOGo mapping.")
            preferences["Vacation"] = {
                "enabled": int(
                    bool(active(vacation.get("active")) and active(mailbox["active"]))
                ),
                "autoReplyText": body,
                "autoReplyEmailAddresses": [username],
                "daysBetweenResponse": days,
                "customSubjectEnabled": 1,
                "customSubject": subject,
                "ignoreLists": 0,
                "alwaysSend": 1,
                "discardMails": 0,
                "startDateEnabled": 1,
                "startDate": start,
                "endDateEnabled": 1,
                "endDate": end,
                "startTimeEnabled": 0,
                "endTimeEnabled": 0,
                "weekdaysEnabled": 0,
            }
        if forwarding is not None:
            delivery = mailbox_delivery(forwarding)
            targets, keep_local = delivery["destinations"], delivery["keepLocal"]
            if targets:
                if any(
                    not re.fullmatch(
                        r"[a-z0-9_.+%=-]+@[a-z0-9.-]+", target, re.IGNORECASE | re.ASCII
                    )
                    for target in targets
                ):
                    issues.append(
                        "Forwarding contains an address that SOGo cannot safely encode."
                    )
                if forwarding["active"] != mailbox["active"]:
                    issues.append("Mailbox and forwarding activity differ.")
                if username in filter_errors:
                    issues.append(filter_errors[username])
                elif keep_local and requires_forwarding_prefilter(
                    converted[username]["filters"]
                ):
                    prefilter = {
                        **mailbox_filter_plans(
                            [],
                            [{"username": username, "destinations": targets}],
                            json_string,
                        )[0],
                        "active": forwarding["active"],
                    }
                else:
                    preferences["Forward"] = {
                        "enabled": active(forwarding["active"]),
                        "forwardAddress": targets,
                        "keepCopy": int(keep_local),
                        "alwaysSend": 1,
                    }
        blockers.extend(f"{username}: {issue}" for issue in issues)
        if preferences or prefilter is not None:
            users.append(
                {
                    "username": username,
                    "preferences": preferences,
                    **({"prefilter": prefilter} if prefilter is not None else {}),
                }
            )
    return {"users": users, "blockers": blockers}
