import json
import math
import re
from decimal import Decimal
from typing import Any

MISSING = object()


def source_truth(value: Any) -> bool:
    return (
        value is not MISSING
        and value is not None
        and value is not False
        and not (
            type(value) in (int, float)
            and (value == 0 or isinstance(value, float) and math.isnan(value))
            or isinstance(value, str)
            and not value
        )
    )


def source_string(value: Any) -> str:
    if value is MISSING:
        return "undefined"
    if value is None:
        return "null"
    if type(value) is bool:
        return "true" if value else "false"
    if isinstance(value, list):
        return ",".join("" if item is None else source_string(item) for item in value)
    if isinstance(value, dict):
        return "[object Object]"
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        if value == 0:
            return "0"
        if 1e-6 <= abs(value) < 1e21:
            text = format(Decimal(str(value)), "f")
            return text[:-2] if text.endswith(".0") else text
        return re.sub(r"e([+-])0+", r"e\1", str(value))
    return str(value)


def source_number(value: Any) -> float:
    if value is MISSING or isinstance(value, dict):
        return math.nan
    if value is None:
        return 0.0
    if isinstance(value, list):
        value = source_string(value)
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return 0.0
        if re.fullmatch(r"0[xX][0-9a-fA-F]+|0[bB][01]+|0[oO][0-7]+", value):
            try:
                return float(int(value, 0))
            except OverflowError:
                return math.inf
        if not re.fullmatch(
            r"[+-]?(?:Infinity|(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)",
            value,
        ):
            return math.nan
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return math.nan


def identity_flag(value: Any) -> Any:
    if type(value) in (bool, int, float) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        if value in ("1", "YES", "true"):
            return True
        if value in ("0", "NO", "false"):
            return False
    return value


def numeric_flag(value: Any) -> bool:
    return (
        type(value) in (bool, int, float)
        and value in (0, 1)
        or isinstance(value, str)
        and value in ("0", "1")
    )


def source_json(value: Any) -> str:
    return (
        "undefined"
        if value is MISSING
        else json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
    )
