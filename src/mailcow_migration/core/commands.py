import json
import logging
import os
import subprocess
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Mapping, Optional, Sequence, Union
from urllib.parse import quote

from .errors import MigrationError
from .storage import local_path, private_directory, write_private_text

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
    timeout: Optional[float] = 120, label: Optional[str] = None, private_output: bool = True,
    secrets: Sequence[str] = (), allowed_codes: Sequence[int] = (0,),
    diagnostic_file: Optional[Path] = None,
    stdout_file: Optional[Path] = None, environment: Optional[Mapping[str, str]] = None,
    inherit_output: bool = False,
) -> CommandResult:
    if not arguments or isinstance(arguments, (str, bytes)) or timeout is not None and timeout <= 0:
        raise MigrationError("A command argument sequence and a positive timeout or None are required")
    if inherit_output and (stdout_file is not None or diagnostic_file is not None):
        raise MigrationError("Inherited command output cannot also use private output files")
    name = label or Path(arguments[0]).name
    LOGGER.debug("Running %s", name)
    try:
        with ExitStack() as stack:
            output: Optional[Union[int, BinaryIO]] = None if inherit_output else subprocess.PIPE
            if stdout_file is not None:
                if os.name != "posix":
                    raise MigrationError("Private command output files require a POSIX host")
                destination = local_path(stdout_file)
                private_directory(destination.parent)
                descriptor = os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                output = stack.enter_context(os.fdopen(descriptor, "wb"))
            process = subprocess.run(
                tuple(arguments), input=input_text, stdin=subprocess.DEVNULL if input_text is None else None,
                stdout=output, stderr=None if inherit_output else subprocess.PIPE, cwd=cwd, timeout=timeout,
                env={**os.environ, **environment} if environment is not None else None,
                encoding="utf-8", check=False,
            )
    except subprocess.TimeoutExpired as error:
        raise MigrationError("{} timed out after {} seconds; inspect its state before repeating changes".format(name, timeout)) from error
    except OSError as error:
        raise MigrationError("Cannot run {}: {}".format(name, error.strerror)) from error
    except UnicodeError as error:
        raise MigrationError("{} returned invalid UTF-8 output; contents omitted".format(name)) from error
    result = CommandResult(process.returncode, process.stdout or "", process.stderr or "")
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
