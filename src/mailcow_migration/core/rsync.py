import re
from pathlib import Path
from typing import Sequence
from uuid import uuid4

from .commands import run_command


def append_maildir(
    source: str,
    destination: Path,
    options: Sequence[str],
    ssh_options: Sequence[str],
    directory: Path,
    *,
    preview: bool,
) -> None:
    flags = (
        *(option for option in options if option != "--itemize-changes"),
        "--ignore-existing",
    )
    paths = (*ssh_options, "--", source, str(destination) + "/")
    print("Counting source entries and planned changes...", flush=True)
    counted = run_command(
        ("rsync", *flags, "--dry-run", "--stats", "--out-format=", *paths),
        timeout=86400,
        label="Read-only rsync count",
        environment={"LC_ALL": "C"},
        diagnostic_file=directory / ("rsync-count-" + uuid4().hex + ".txt"),
    )
    for line in counted.stdout.splitlines():
        if re.match(
            r"^(Number of (files|regular files transferred|files transferred|created files|deleted files):|"
            r"Total (file|transferred file) size:)",
            line,
        ):
            print(line, flush=True)
    run_command(
        (
            "rsync",
            *flags,
            "--dry-run" if preview else "--info=progress2",
            "--itemize-changes",
            "--itemize-changes",
            "--out-format=Maildir entry: %n%L | %l bytes | %i",
            "--stats",
            *paths,
        ),
        timeout=86400,
        label="Maildir append preview" if preview else "Maildir append",
        environment={"LC_ALL": "C"},
        inherit_output=True,
    )
