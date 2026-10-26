import ipaddress
from pathlib import Path
from typing import Any, Mapping, Optional

from ..core.errors import ConfigurationError
from ..core.storage import read_json


def load_policy(file: Optional[str]) -> Mapping[str, Any]:
    policy = read_json(Path(file), private=True) if file else {}
    allowed = {"adminNames", "allDomainAdmins", "mergeAdmins", "vacationZeroIntervalDays", "vacationDatesAreUtc",
               "mailcowIp", "senderPermissions", "sourcePasswordExpiration", "vacationTimeZone"}
    if not isinstance(policy, dict) or set(policy) - allowed:
        raise ConfigurationError("Unknown settings policy field; use settings-policy.example.json")
    for name, choices in (("allDomainAdmins", ("review", "batch-only")), ("sourcePasswordExpiration", ("disabled", "enabled", "review"))):
        if name in policy and policy[name] not in choices:
            raise ConfigurationError("{} must be {}".format(name, ", ".join(choices)))
    names = policy.get("adminNames", {})
    if not isinstance(names, dict) or any(not isinstance(value, str) or not value for value in names.values()):
        raise ConfigurationError("adminNames must map source names to nonempty target names")
    merges = policy.get("mergeAdmins", [])
    if not isinstance(merges, list) or any(not isinstance(name, str) for name in merges):
        raise ConfigurationError("mergeAdmins must contain explicitly reviewed target administrator names")
    if "vacationDatesAreUtc" in policy and type(policy["vacationDatesAreUtc"]) is not bool:
        raise ConfigurationError("vacationDatesAreUtc must be boolean")
    interval = policy.get("vacationZeroIntervalDays")
    if interval is not None and (type(interval) is not int or not 1 <= interval <= 30):
        raise ConfigurationError("vacationZeroIntervalDays must be null or a reviewed integer from 1 to 30")
    zone = policy.get("vacationTimeZone", "Europe/Rome")
    if not isinstance(zone, str) or not zone:
        raise ConfigurationError("vacationTimeZone must be a nonempty IANA timezone name")
    if "mailcowIp" in policy:
        try:
            if not isinstance(policy["mailcowIp"], str):
                raise ValueError("Expected IP address text")
            ipaddress.ip_address(policy["mailcowIp"])
        except ValueError as error:
            raise ConfigurationError("mailcowIp must be a fixed IP address") from error
    senders = policy.get("senderPermissions", {})
    if (not isinstance(senders, dict) or any(not isinstance(items, list) or any(not isinstance(item, str) for item in items)
                                           for items in senders.values())):
        raise ConfigurationError("senderPermissions must map users to address lists")
    return {"sourcePasswordExpiration": "disabled", "vacationTimeZone": "Europe/Rome", **policy}


