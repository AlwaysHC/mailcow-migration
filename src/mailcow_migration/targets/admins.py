from typing import Any, Mapping, Optional, Sequence

from .reconciliation import api_number
from ..core.errors import MigrationError


def domain_administrator(planned: Mapping[str, Any], inventory: Sequence[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    username = planned["username"]
    matches = [row for row in inventory if row.get("username") == username]
    if len(matches) > 1:
        raise MigrationError("Duplicate target domain administrator: " + username)
    if not matches:
        return None
    found = matches[0]
    domains = found.get("selected_domains")
    if (not isinstance(domains, list) or any(not isinstance(value, str) for value in domains)
            or api_number(found.get("active")) != planned["active"]):
        raise MigrationError(username + ": incompatible administrator activity/membership data; no overwrite performed")
    return found


def verify_administrator(
    planned: Mapping[str, Any], expected: Mapping[str, Any], found: Optional[Mapping[str, Any]], *, exact: bool = False,
) -> None:
    attributes = found.get("attributes") if found is not None and isinstance(found.get("attributes"), dict) else {}
    if (found is None or not set([*expected["preserved"], *planned["domains"]]) <= set(found["selected_domains"])
            or exact and set(found["selected_domains"]) != set(expected["domains"])
            or any(api_number(attributes.get(key)) != value for key, value in expected.get("security", {}).items())):
        raise MigrationError(planned["username"] + ": administrator membership/security readback differs; retain the journal")


def verify_administrators(
    planned: Sequence[Mapping[str, Any]], expected: Mapping[str, Any], inventory: Sequence[Mapping[str, Any]], *, exact: bool = False,
) -> None:
    for row in planned:
        verify_administrator(row, expected[row["username"]], domain_administrator(row, inventory), exact=exact)
