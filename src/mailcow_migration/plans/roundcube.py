import json
import re
from typing import Any, Callable, Mapping

from ..core.errors import MigrationError
from ..core.folders import collapsed_folders, folder_path
from ..core.storage import json_fingerprint
from ..core.timezones import timezone_parts
from .roundcube_contacts import (
    collected_contact_record,
    contact_record,
    normalized_card,
)
from .roundcube_preferences import user_preferences


def snapshot_blockers(snapshot: Mapping[str, Any]) -> list:
    return [
        *(
            f"Database table {table} has unknown user-data ownership/conversion"
            for table in snapshot["unknownTables"]
        ),
        *(
            ["Shared dictionary records have no implemented SOGo conversion"]
            if snapshot["sharedDictionary"]
            else []
        ),
        *(
            "{}: {} {} records have no implemented SOGo conversion".format(
                user["login"], len(user[key]), key
            )
            for user in snapshot["users"]
            for key in ("groups", "responses", "dictionary", "searches", "files")
            if user[key]
        ),
    ]


def roundcube_plan(
    snapshot: Mapping[str, Any],
    source: str,
    account_plan: Mapping[str, Any],
    account_fingerprint: str,
    inspect_folders: Callable[[str], Mapping[str, Any]],
) -> Mapping[str, Any]:
    blockers = snapshot_blockers(snapshot)
    users, deferred, timezones = [], [], {}
    targets = {row["username"] for row in account_plan["mailboxes"]}

    def validate_timezone(name: str) -> None:
        if name not in timezones:
            try:
                timezone_parts(name, ())
                timezones[name] = None
            except MigrationError as error:
                timezones[name] = str(error)
        if timezones[name] is not None:
            raise MigrationError(timezones[name])

    for user in snapshot["users"]:
        if user["target"] not in targets or not re.fullmatch(
            r"[a-zA-Z0-9_.+%-]+@[a-zA-Z0-9.-]+", user["target"]
        ):
            blockers.append("Unsupported RoundCube target username: " + user["target"])
            continue
        origin = {
            "source": source,
            "directory": snapshot["directory"],
            "database": snapshot["database"],
            "user": user["id"],
        }
        settings = user_preferences(user, snapshot["defaults"] or {}, validate_timezone)
        blockers.extend(settings["blockers"])
        deferred.extend(settings["deferredPreferences"])
        records = []
        for rows, mapper in (
            (user["contacts"], contact_record),
            (user["collectedAddresses"], collected_contact_record),
        ):
            for row in rows:
                if not isinstance(row, dict):
                    blockers.append(user["login"] + ": invalid contact record")
                    continue
                try:
                    records.append(mapper(row, origin))
                except MigrationError as error:
                    blockers.append(user["login"] + ": " + str(error))
        identifiers = [
            next(
                line
                for line in normalized_card(row["c_content"]).split("\n")
                if re.match(r"UID[;:]", line, re.IGNORECASE)
            )
            for row in records
        ]
        if len(set(identifiers)) != len(identifiers):
            blockers.append(
                user["login"] + ": duplicate contact UIDs need reconciliation"
            )
        language = settings["language"]
        native = [
            *settings["nativeBehaviors"],
            *(
                [
                    "RoundCube English (UK) maps to SOGo English; there is no separate UK translation."
                ]
                if language == "en_GB"
                else []
            ),
        ]
        users.append(
            {
                "username": user["target"],
                "roundcubeUser": user["id"],
                "book": "roundcube-" + json_fingerprint(origin)[:24],
                "records": records,
                "preferences": settings["preferences"],
                "nativeBehaviors": native,
                "collectedRecipients": len(user["collectedAddresses"]),
                "language": language,
                "groups": len(user["groups"]),
                "identities": len(settings["preferences"]["SOGoMailIdentities"]),
                "folderState": None,
            }
        )
    if not blockers:
        for user, proposal in zip(snapshot["users"], users):
            preferences = user["preferences"] or {}
            if "collapsed_folders" not in preferences:
                continue
            try:
                inventory = inspect_folders(user["target"])
                collapsed = [
                    folder_path(name, inventory["separator"])
                    for name in collapsed_folders(preferences["collapsed_folders"])
                ]
                missing = [
                    name for name in collapsed if name not in inventory["folders"]
                ]
                if missing:
                    raise MigrationError(
                        "Collapsed folders are absent from the source inventory: "
                        + ", ".join(missing)
                    )
                proposal["folderState"] = {
                    "source": inventory,
                    "collapsed": collapsed,
                    "target_verified": False,
                }
            except MigrationError as error:
                blockers.append(user["target"] + ": " + str(error))
    return {
        "version": 1,
        "kind": "roundcube-sogo-proposal",
        "preservation_version": 3,
        "account_plan_sha256": account_fingerprint,
        "source_sha256": json_fingerprint(snapshot, canonical=True),
        "source": {
            "host": source,
            "directory": snapshot["directory"],
            "database": snapshot["database"],
        },
        "defaults": {
            "automatic_timezone": "Europe/Rome",
            "blank_identity_name": "email address",
            "missing_default_identity": "prefer the mailbox identity or create one",
        },
        "users": users,
        "ignoredUsers": snapshot["ignoredUsers"],
        "blockers": list(dict.fromkeys(blockers)),
        "deferredPreferences": deferred,
        "target_verified": False,
        "import_complete": False,
    }


def roundcube_summary(proposal: Mapping[str, Any]) -> str:
    lines = [
        "RoundCube to SOGo source proposal",
        "Source snapshot: " + proposal["source_sha256"],
        "Default timezone: Europe/Rome when automatic or missing; blank identity names use the email address.",
        "When no default identity exists, prefer the mailbox identity or create one.",
        "",
    ]
    for user in proposal["users"]:
        lines.extend(
            (
                "{}: {} contacts; {} identities; RoundCube profile {}".format(
                    user["username"],
                    len(user["records"]),
                    user["identities"],
                    user["roundcubeUser"],
                ),
                json.dumps(
                    user["preferences"], ensure_ascii=False, indent=2, allow_nan=False
                ),
            )
        )
        lines.extend("  " + note for note in user["nativeBehaviors"])
        folders = user["folderState"]
        if folders is not None:
            lines.append(
                "  Folder display: collapse {}; expand remaining target folders after copy and target verification.".format(
                    ", ".join(folders["collapsed"]) if folders["collapsed"] else "none"
                )
            )
        lines.append("")
    lines.extend("BLOCKED: " + reason for reason in proposal["blockers"])
    lines.extend(
        "DEFERRAL REQUIRES APPROVAL: {}: {}={} -> {}".format(
            row["username"],
            row["preference"],
            json.dumps(row["value"], ensure_ascii=False),
            row["fallback"],
        )
        for row in proposal["deferredPreferences"]
    )
    lines.extend(
        (
            "",
            "Contacts and all proposed values are retained in the matching private JSON proposal.",
            "Source review only: target capability/conflict checks, folder verification after copying and import remain pending.",
            "Feature references are informational; they do not establish support in the installed target.",
        )
    )
    return "\n".join(lines) + "\n"
