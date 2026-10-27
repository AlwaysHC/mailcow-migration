import re
from pathlib import Path
from typing import Any, Mapping

from ..core.errors import MigrationError
from ..core.storage import read_text

ESCAPES = {"0": "\0", "b": "\b", "n": "\n", "r": "\r", "t": "\t", "Z": "\x1a"}
TOKEN = re.compile(r"\s*(?:'((?:\\[\s\S]|[^'\\])*)'|([^,)]*))")


def parse_values(payload: str) -> list:
    rows, index = [], 0
    while index < len(payload):
        while index < len(payload) and (
            payload[index].isspace() or payload[index] == ","
        ):
            index += 1
        if index == len(payload):
            break
        if payload[index] != "(":
            raise MigrationError("Expected SQL tuple; export contents omitted")
        index += 1
        values = []
        while True:
            match = TOKEN.match(payload, index)
            if match is None:
                raise MigrationError("Invalid SQL value; export contents omitted")
            quoted, raw = match.groups()
            if quoted is not None:
                value = re.sub(
                    r"\\([\s\S])", lambda item: ESCAPES.get(item[1], item[1]), quoted
                )
            else:
                raw = raw.strip()
                if not raw or raw.startswith("'"):
                    raise MigrationError(
                        "Invalid or unterminated SQL value; export contents omitted"
                    )
                value = (
                    None
                    if raw.upper() == "NULL"
                    else int(raw)
                    if re.fullmatch(r"-?\d+", raw)
                    else float(raw)
                    if re.fullmatch(r"-?(?:\d+\.\d*|\d*\.\d+)", raw)
                    else raw
                )
            values.append(value)
            index = match.end()
            while index < len(payload) and payload[index].isspace():
                index += 1
            if index >= len(payload) or payload[index] not in ",)":
                raise MigrationError("Expected SQL delimiter; export contents omitted")
            delimiter = payload[index]
            index += 1
            if delimiter == ")":
                break
        rows.append(values)
    return rows


def parse_sql(file: Path) -> Mapping[str, Any]:
    text = read_text(file, private=True)
    definitions = dict(
        re.findall(
            r"CREATE TABLE `([^`]+)` \(\r?\n(.*?)\r?\n\) ENGINE=", text, re.DOTALL
        )
    )
    schemas = {
        table: re.findall(r"^\s*`([^`]+)`\s+", definition, re.MULTILINE)
        for table, definition in definitions.items()
    }
    rows = {table: [] for table in schemas}
    for table, payload in re.findall(
        r"^INSERT INTO `([^`]+)` VALUES (.*);\r?$", text, re.MULTILINE
    ):
        if table not in schemas:
            raise MigrationError("SQL insert has no exported schema")
        for values in parse_values(payload):
            if len(values) != len(schemas[table]):
                raise MigrationError("SQL value count differs from its exported schema")
            rows[table].append(dict(zip(schemas[table], values)))
    vacation_insert = text.find("INSERT INTO `vacation`")
    zones = re.findall(
        r"SET TIME_ZONE\s*=\s*'([^']+)'",
        text if vacation_insert < 0 else text[:vacation_insert],
    )
    dates_utc = bool(
        zones
        and zones[-1] == "+00:00"
        and all(
            re.search(
                r"`" + column + r"`\s+timestamp",
                definitions.get("vacation", ""),
                re.IGNORECASE,
            )
            for column in ("activefrom", "activeuntil")
        )
    )
    return {"schemas": schemas, "rows": rows, "vacationDatesUtc": dates_utc}
