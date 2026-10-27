from typing import Any, Mapping, Sequence

from ..core.errors import MigrationError
from .reconciliation import api_number


def transport_matches(row: Mapping[str, Any], desired: Mapping[str, Any]) -> bool:
    return (
        str(row.get("destination") or "").lower() == desired["destination"]
        and row.get("nexthop") == desired["nexthop"]
        and api_number(row.get("active")) == 1
        and api_number(row.get("is_mx_based")) == 0
        and (row.get("username") or "") == desired["username"]
        and (row.get("password") or "") == desired["password"].replace(":", "\\:")
    )


def inspect_transport(
    payload: Mapping[str, Any],
    routes: Sequence[Mapping[str, Any]],
    domains: Sequence[Mapping[str, Any]],
) -> bool:
    matches = tuple(
        row
        for row in routes
        if str(row.get("destination") or "").lower() == payload["destination"]
    )
    if len(matches) > 1 or matches and not transport_matches(matches[0], payload):
        raise MigrationError("Target transport conflicts for " + payload["destination"])
    if any(
        (row.get("domain_name") or row.get("domain")) == payload["destination"]
        and api_number(row.get("relay_all_recipients")) != 1
        for row in domains
    ):
        raise MigrationError(
            "Source transport conflicts with a target local domain: "
            + payload["destination"]
        )
    if any(
        row.get("nexthop") == payload["nexthop"]
        and (
            (row.get("username") or "") != payload["username"]
            or (row.get("password") or "") != payload["password"].replace(":", "\\:")
        )
        for row in routes
    ):
        raise MigrationError(
            "Target relay credentials conflict for " + payload["destination"]
        )
    return bool(matches)
