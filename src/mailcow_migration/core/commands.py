import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence
from urllib.parse import quote

from .errors import MigrationError
from .storage import write_private_text

LOGGER = logging.getLogger("mailcow_migration")


def redact(text: str, secrets: Sequence[str]) -> str:
    variants = {variant for secret in secrets if secret for variant in (secret, json.dumps(secret)[1:-1], quote(secret, safe=""))}
    for secret in sorted(variants, key=len, reverse=True):
        text = text.replace(secret, "[redacted]")
    return text


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = field(repr=False)
    stderr: str = field(repr=False)


def run_command(
    arguments: Sequence[str], *, input_text: Optional[str] = None, cwd: Optional[Path] = None,
    timeout: float = 120, label: Optional[str] = None, private_output: bool = True,
    secrets: Sequence[str] = (), allowed_codes: Sequence[int] = (0,),
    diagnostic_file: Optional[Path] = None,
) -> CommandResult:
    if not arguments or isinstance(arguments, (str, bytes)) or timeout <= 0:
        raise MigrationError("A command argument sequence and positive timeout are required")
    name = label or Path(arguments[0]).name
    LOGGER.debug("Running %s", name)
    try:
        process = subprocess.run(
            tuple(arguments), input=input_text, stdin=subprocess.DEVNULL if input_text is None else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, timeout=timeout,
            encoding="utf-8", check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise MigrationError("{} timed out after {} seconds; inspect its state before repeating changes".format(name, timeout)) from error
    except OSError as error:
        raise MigrationError("Cannot run {}: {}".format(name, error.strerror)) from error
    except UnicodeError as error:
        raise MigrationError("{} returned invalid UTF-8 output; contents omitted".format(name)) from error
    result = CommandResult(process.returncode, process.stdout, process.stderr)
    if result.stderr:
        if diagnostic_file is not None:
            write_private_text(diagnostic_file, result.stderr)
            diagnostic = "diagnostics saved privately to {}".format(diagnostic_file)
        else:
            diagnostic = "private diagnostic output received" if private_output else redact(result.stderr.rstrip(), secrets)
        LOGGER.warning("%s: %s", name, diagnostic)
    if result.returncode not in allowed_codes:
        raise MigrationError("{} failed with exit status {}".format(name, result.returncode))
    return result
