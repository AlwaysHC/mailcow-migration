import json
import re
import sys
from typing import Iterable, Mapping

from .commands import run_command
from .errors import MigrationError
from .storage import decode_json
from ..resources import read_resource


def timezone_parts(name: str, timestamps: Iterable[int]) -> Mapping[int, Mapping[str, str]]:
    values = tuple(dict.fromkeys(timestamps))
    result = run_command(
        (sys.executable, "-c", read_resource("timezone-parts.py")),
        input_text=json.dumps({"timeZone": name, "timestamps": values}, allow_nan=False),
        label="Vacation timezone assessment", private_output=False,
    )
    try:
        parts = decode_json(result.stdout)
        fields = {"year", "month", "day", "hour", "minute", "second"}
        if (not isinstance(parts, list) or len(parts) != len(values) or any(
            not isinstance(row, dict) or set(row) != fields or any(
                not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}" if key == "year" else r"[0-9]{2}", value)
                for key, value in row.items()) for row in parts
        )):
            raise ValueError("Invalid timezone result")
    except (ValueError, UnicodeError) as error:
        raise MigrationError("Vacation timezone assessment returned invalid date parts") from error
    return dict(zip(values, parts))
