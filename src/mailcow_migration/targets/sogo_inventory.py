from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from ..core.errors import MigrationError
from ..core.storage import json_fingerprint, write_private_json
from ..plans.roundcube_contacts import normalized_card
from ..plans.roundcube_values import MISSING
from .sogo_values import preference_equal


def check_preferences(
    backup: Mapping[str, Any],
    user: Mapping[str, Any],
    directory: Path,
    journal: Optional[Mapping[str, Any]] = None,
) -> Mapping[str, Any]:
    preferences = backup.get("preferences")
    current = preferences[0] if isinstance(preferences, list) and preferences else None
    if not isinstance(current, dict):
        raise MigrationError(
            user["username"] + ": SOGo backup has no user-defaults dictionary"
        )
    completed = journal.get("preferences", {}) if journal else {}
    for key, expected in user["preferences"].items():
        if not preference_equal(key, current.get(key, MISSING), expected) and (
            key in current or completed.get(key) == "complete"
        ):
            file = directory / (
                "preference-conflict-" + json_fingerprint(user["username"]) + ".json"
            )
            write_private_json(
                file,
                {
                    "username": user["username"],
                    "key": key,
                    "stage": completed.get(key, "not written by this migration"),
                    "expected": expected,
                    "actual": current.get(key),
                    "actualPresent": key in current,
                },
                replace=True,
            )
            raise MigrationError(
                "{}: SOGo {} differs from the approved proposal; no conflicting value was overwritten. "
                "Private comparison: {}. Retain the state/backups and reconcile the difference.".format(
                    user["username"], key, file
                )
            )
    return current


def check_book(
    backup: Mapping[str, Any],
    username: str,
    book: str,
    records: Sequence[Mapping[str, str]],
    *,
    owned: bool,
) -> list:
    tables = backup.get("tables")
    if not isinstance(tables, dict):
        raise MigrationError(username + ": SOGo backup has no folder inventory")
    found = tables.get(f"/Users/{username}/Contacts/{book}")
    if found is None:
        return []
    if not owned:
        raise MigrationError(
            username
            + ": destination RoundCube address book already exists without this migration's import journal"
        )
    if (
        not isinstance(found, dict)
        or found.get("displayname") != "Roundcube contacts"
        or found.get("acl") not in ({}, [])
        or not isinstance(found.get("records"), list)
    ):
        raise MigrationError(
            username
            + ": migration address book metadata/sharing changed; stop before overwriting it"
        )
    expected = {row["c_name"]: row["c_content"] for row in records}
    seen = set()
    for row in found["records"]:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("c_name"), str)
            or row["c_name"] in seen
            or row["c_name"] not in expected
            or not isinstance(row.get("c_content"), str)
            or normalized_card(row["c_content"])
            != normalized_card(expected[row["c_name"]])
        ):
            raise MigrationError(
                username
                + ": target contact differs from the saved import; no overwrite performed"
            )
        seen.add(row["c_name"])
    return sorted(seen)
