import math
from typing import Any, Mapping

from ..core.errors import MigrationError
from ..plans.roundcube_values import MISSING, identity_flag, source_string


def property_list(value: Any) -> str:
    if type(value) is bool:
        return "YES" if value else "NO"
    if type(value) in (int, float):
        if not math.isfinite(value):
            raise MigrationError("Cannot encode a non-finite GNUstep property-list number")
        return source_string(value)
    if value is None or value is MISSING:
        raise MigrationError("Cannot encode a null GNUstep property-list value")
    if isinstance(value, str):
        escapes = {34: '\\"', 92: "\\\\", 10: "\\n", 13: "\\r", 9: "\\t"}
        encoded = value.encode("utf-16-be", errors="surrogatepass")
        units = (int.from_bytes(encoded[index:index + 2], "big") for index in range(0, len(encoded), 2))
        return '"' + "".join(escapes.get(unit, "\\U{:04x}".format(unit) if unit >= 128 else chr(unit)) for unit in units) + '"'
    if isinstance(value, list):
        return "(" + ",".join(property_list(item) for item in value) + ")"
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return "{" + "".join(property_list(key) + "=" + property_list(item) + ";" for key, item in value.items()) + "}"
    raise MigrationError("Unsupported GNUstep property-list value")


def configuration_value(config: Mapping[str, Any], domain: str, key: str) -> Any:
    domains = config.get("domains") or {}
    if not isinstance(domains, dict):
        raise MigrationError("SOGo returned an invalid domain configuration")
    values = domains.get(domain) or {}
    if not isinstance(values, dict):
        raise MigrationError("SOGo returned invalid overrides for " + domain)
    return values[key] if values.get(key) is not None else config.get(key)


def enabled(value: Any) -> bool:
    return source_string(value).lower() in ("yes", "true", "1")


def preference_value(key: str, value: Any) -> Any:
    if key == "SOGoSieveFilters" and isinstance(value, list):
        return [{**row, "active": identity_flag(row.get("active", MISSING))} if isinstance(row, dict) else row for row in value]
    if key == "SOGoMailIdentities" and isinstance(value, list):
        return [{**row, "isDefault": identity_flag(row["isDefault"])} if isinstance(row, dict) and "isDefault" in row else row for row in value]
    return identity_flag(value) if key == "SOGoMailSortByThreads" else value


def equal_values(left: Any, right: Any) -> bool:
    if left is MISSING or right is MISSING:
        return left is right
    if type(left) is bool or type(right) is bool:
        return type(left) is type(right) and left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(equal_values(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(equal_values(a, b) for a, b in zip(left, right))
    return left == right


def preference_equal(key: str, left: Any, right: Any) -> bool:
    return equal_values(preference_value(key, left), preference_value(key, right))
