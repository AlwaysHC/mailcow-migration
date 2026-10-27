import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Tuple

from ..configuration import optional_path, ssh_host
from .commands import CommandResult, run_command
from .errors import MigrationError
from .storage import local_path


@dataclass(frozen=True)
class SshConnection:
    host: str
    configuration_file: Optional[str] = None

    def __post_init__(self) -> None:
        ssh_host(self.host)
        optional_path(self.configuration_file)

    def options(self) -> Tuple[str, ...]:
        configuration = (
            ("-F", self.configuration_file) if self.configuration_file else ()
        )
        return (
            *configuration,
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=15",
        )

    def arguments(self) -> Tuple[str, ...]:
        return ("ssh", "-T", *self.options())

    def run(
        self,
        arguments: Sequence[str],
        *,
        input_text: Optional[str] = None,
        timeout: float = 120,
        private_output: bool = True,
        secrets: Sequence[str] = (),
        diagnostic_file: Optional[Path] = None,
        allowed_codes: Sequence[int] = (0,),
    ) -> CommandResult:
        if not arguments or isinstance(arguments, (str, bytes)):
            raise MigrationError("Remote commands require an argument sequence")
        remote = " ".join(shlex.quote(argument) for argument in arguments)
        return run_command(
            (*self.arguments(), self.host, remote),
            input_text=input_text,
            timeout=timeout,
            label=f"SSH {self.host} ({arguments[0]})",
            private_output=private_output,
            secrets=secrets,
            diagnostic_file=diagnostic_file,
            allowed_codes=allowed_codes,
        )

    def copy_to(
        self,
        files: Sequence[Path],
        remote_directory: str,
        diagnostic_file: Optional[Path] = None,
    ) -> None:
        optional_path(remote_directory)
        if not files:
            raise MigrationError("SCP upload requires at least one file")
        run_command(
            (
                "scp",
                "-O",
                *self.options(),
                "--",
                *(str(local_path(file)) for file in files),
                f"{self.host}:{shlex.quote(remote_directory)}/",
            ),
            timeout=120,
            label=f"SCP helper upload to {self.host}",
            diagnostic_file=diagnostic_file,
        )

    def copy_from(
        self,
        remote_directory: str,
        destination: Path,
        diagnostic_file: Optional[Path] = None,
    ) -> None:
        optional_path(remote_directory)
        run_command(
            (
                "scp",
                "-O",
                *self.options(),
                "-r",
                "--",
                f"{self.host}:{shlex.quote(remote_directory)}",
                str(local_path(destination)),
            ),
            timeout=3600,
            label=f"SCP export download from {self.host}",
            diagnostic_file=diagnostic_file,
        )

    def rsync_arguments(self) -> Tuple[str, str]:
        if self.configuration_file and any(
            char in self.configuration_file for char in "\r\n\"'\\"
        ):
            raise MigrationError(
                "SSH configuration path contains unsupported rsync quoting characters"
            )
        command = "ssh -o BatchMode=yes -o StrictHostKeyChecking=yes"
        if self.configuration_file:
            command += f' -F "{self.configuration_file}"'
        return "--rsh", command
