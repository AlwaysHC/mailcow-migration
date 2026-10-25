import re
from typing import Any

from .errors import ConfigurationError


def domain_name(value: Any) -> str:
    if (not isinstance(value, str) or len(value) > 253 or "." not in value
            or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in value.split("."))):
        raise ConfigurationError("Use a lowercase domain name without a trailing dot; use ASCII punycode for international domains")
    return value
