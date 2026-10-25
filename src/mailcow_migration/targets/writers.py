import os
import re
from pathlib import Path
from typing import Sequence

from ..core.errors import MigrationError


def check_mailbox_writers(root: Path, usernames: Sequence[str], domains: Sequence[str]) -> None:
    roots = tuple(prefix for username in usernames for local, domain in (username.split("@"),)
                  for prefix in ("{}/{}/{}/".format(root, domain, local), "/var/vmail/{}/{}/".format(domain, local)))
    schedules = (Path("/etc/crontab"), *(file for directory in ("/etc/cron.d", "/var/spool/cron", "/var/spool/cron/crontabs")
                                      if Path(directory).exists() for file in Path(directory).iterdir()))
    references = tuple(re.compile(r"(?<![a-zA-Z0-9_.+%@/-])" + re.escape(value) + r"(?![a-zA-Z0-9_.+%@-])")
                       for value in (*usernames, *roots, *domains))
    for schedule in schedules:
        if not schedule.is_file():
            continue
        for index, line in enumerate(schedule.read_text(encoding="utf-8").splitlines(), 1):
            if re.match(r"\s*(?:#|$)", line) or not re.search(r"\b(?:imapsync|fetchmail|getmail|offlineimap|mbsync|dsync|rsync)\b", line):
                continue
            if any(pattern.search(line) for pattern in references):
                raise MigrationError("Scheduled target writer references this batch: {}:{}. Its ownership may include other domains; "
                                     "a scoped pause decision is required before changing this job.".format(schedule, index))
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            for descriptor in (process / "fd").iterdir():
                try:
                    target = os.readlink(str(descriptor))
                    if not target.startswith(roots):
                        continue
                    flags = re.search(r"^flags:\s+([0-7]+)", (process / "fdinfo" / descriptor.name).read_text(encoding="utf-8"), re.M)
                    if not flags or int(flags[1], 8) & 3:
                        raise MigrationError("Mailcow mailbox writer still open: PID {}, {}".format(process.name, target))
                except (FileNotFoundError, ProcessLookupError):
                    continue
        except (FileNotFoundError, ProcessLookupError):
            continue
