import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..configuration import MigrationConfiguration
from ..core.errors import MigrationError
from ..plans.domain import MIB, QUOTA_LIMIT, SAFE_INTEGER, number
from ..resources import read_resource
from .inventory import SourceInspection
from .maildirs import mailbox_path, source_identity


def usage_quota(
    usage: Mapping[str, Any], source_quota_bytes: Any, policy: Mapping[str, int]
) -> int:
    source_quota = number(source_quota_bytes)
    values = [usage["messages"], usage["bytes"], usage["upperBoundBytes"], source_quota]
    if (
        any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0 <= value <= SAFE_INTEGER
            or int(value) != value
            for value in values
        )
        or usage["upperBoundBytes"] < usage["bytes"]
    ):
        raise MigrationError("Invalid source mailbox size or source quota")
    quota = max(
        policy["minimum"],
        math.ceil(usage["upperBoundBytes"] / MIB * (1 + policy["headroom"] / 100)),
        math.ceil(source_quota / MIB),
    )
    if quota > QUOTA_LIMIT:
        raise MigrationError("Calculated mailbox quota is too large")
    return quota


def measure_source(
    plan: Mapping[str, Any],
    source: SourceInspection,
    settings: MigrationConfiguration,
    directory: Path,
) -> Mapping[str, Any]:
    policy = {
        "headroom": settings.quota_headroom_percent,
        "minimum": settings.quota_min_mib,
    }
    script = read_resource("measure-mailbox.sh")
    mailboxes = []
    for index, row in enumerate(plan["mailboxes"]):
        payload = row["payload"]
        path = mailbox_path(source, payload["domain"], payload["local_part"])
        print("Measuring {}, including compressed messages...".format(row["username"]))
        result = source.connection.ssh.run(
            ("bash", "-s", "--", str(path), row["username"], "0"),
            input_text=script,
            timeout=86400,
            diagnostic_file=directory / f"measurement-{index + 1}.txt",
        )
        match = re.fullmatch(
            r"(\d+)\t(\d+)\t(\d+)(?:\t(\d+)(?:\t(\d+))?)?\s*", result.stdout
        )
        if match is None:
            raise MigrationError("Invalid source size response: " + row["username"])
        usage = {
            "messages": int(match[1]),
            "bytes": int(match[2]),
            "upperBoundBytes": int(match[3]),
            "metadataMessages": int(match[4] or 0),
            "storedBytes": int(match[5]) if match[5] is not None else None,
        }
        if (
            usage["metadataMessages"] > usage["messages"]
            or usage["metadataMessages"] > SAFE_INTEGER
            or usage["storedBytes"] is not None
            and usage["storedBytes"] > SAFE_INTEGER
        ):
            raise MigrationError(
                "Invalid source metadata or stored byte count: " + row["username"]
            )
        source_mailbox = next(
            item
            for item in plan["source"]["mailboxes"]
            if item["username"] == row["username"]
        )
        quota = usage_quota(usage, source_mailbox["quota"], policy)
        mailboxes.append({"username": row["username"], **usage, "quota": quota})
        print(
            "  {} messages; {:.2f} MiB; quota {} MiB.".format(
                usage["messages"], usage["bytes"] / MIB, quota
            )
        )
    return {
        "source": source_identity(source),
        "measuredAt": datetime.now(timezone.utc).isoformat(),
        "policy": policy,
        "mailboxes": mailboxes,
    }


def apply_source_usage(
    plan: Mapping[str, Any], usage: Mapping[str, Any]
) -> Mapping[str, Any]:
    if len(usage["mailboxes"]) != len(plan["mailboxes"]):
        raise MigrationError("Source measurement does not match the batch")
    mailboxes = []
    for row in plan["mailboxes"]:
        matches = [
            item for item in usage["mailboxes"] if item["username"] == row["username"]
        ]
        if len(matches) != 1:
            raise MigrationError(
                "Missing or duplicate source measurement: " + row["username"]
            )
        source_mailbox = next(
            item
            for item in plan["source"]["mailboxes"]
            if item["username"] == row["username"]
        )
        quota = usage_quota(matches[0], source_mailbox["quota"], usage["policy"])
        mailboxes.append({**row, "payload": {**row["payload"], "quota": quota}})
    maximum = max(
        [usage["policy"]["minimum"], *(row["payload"]["quota"] for row in mailboxes)]
    )
    total = sum(row["payload"]["quota"] for row in mailboxes) or maximum
    if total > QUOTA_LIMIT:
        raise MigrationError("Calculated domain quota is too large")
    return {
        **plan,
        "quota": maximum,
        "mailboxes": mailboxes,
        "sourceUsage": usage,
        "domainPayload": {
            **plan["domainPayload"],
            "defquota": usage["policy"]["minimum"],
            "maxquota": maximum,
            "quota": total,
        },
        "notes": [
            *[
                note
                for note in plan["notes"]
                if not note.startswith("Verify selected quota")
            ],
            "Quotas use source sizes plus a conservative CRLF allowance, headroom and existing finite limits; review growth before cutover",
            *[
                "{}: {} emails sized from Maildir metadata; verify target message readability in the pilot".format(
                    row["username"], row["metadataMessages"]
                )
                for row in usage["mailboxes"]
                if row["metadataMessages"]
            ],
        ],
    }
