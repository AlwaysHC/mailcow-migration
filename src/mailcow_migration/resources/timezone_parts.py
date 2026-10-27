import json
import os
import re
import sys
import time
from pathlib import Path


def timezone_file(name):
    if (
        not isinstance(name, str)
        or not re.fullmatch(r"[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*", name)
        or name.split("/")[0].lower() in ("posix", "right", "localtime", "posixrules")
    ):
        raise ValueError("Invalid vacationTimeZone; use an IANA timezone name")
    directory = Path(os.environ.get("TZDIR", "/usr/share/zoneinfo"))
    for part in name.split("/"):
        matches = [
            entry for entry in directory.iterdir() if entry.name.lower() == part.lower()
        ]
        if len(matches) != 1:
            raise ValueError(
                "Unknown vacationTimeZone; check the name and installed host timezone data"
            )
        directory = matches[0]
    with directory.open("rb") as stream:
        if stream.read(4) != b"TZif":
            raise ValueError("Invalid host timezone data for vacationTimeZone")
    return directory.resolve()


def main():
    request = json.load(sys.stdin)
    file = timezone_file(request["timeZone"])
    timestamps = request["timestamps"]
    if not isinstance(timestamps, list) or any(
        type(value) is not int or not 0 <= value <= 2147483647 for value in timestamps
    ):
        raise ValueError("Invalid vacation timestamps")
    if not hasattr(time, "tzset"):
        raise ValueError("Vacation timezone assessment requires a POSIX host")
    os.environ["TZ"] = ":" + str(file)
    time.tzset()
    fields = ("year", "month", "day", "hour", "minute", "second")
    result = [
        dict(
            zip(
                fields,
                time.strftime("%Y %m %d %H %M %S", time.localtime(value)).split(),
            )
        )
        for value in timestamps
    ]
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except (KeyError, TypeError, ValueError, OSError) as error:
        detail = error.strerror if isinstance(error, OSError) else str(error)
        print(f"Vacation timezone assessment failed: {detail}", file=sys.stderr)
        sys.exit(1)
