import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from ..configuration import WORK_ROOT
from .errors import MigrationError, OperationLocked
from .storage import local_path, private_directory, read_json, write_private_json


@dataclass(frozen=True)
class Workspace:
    root: Path = Path(WORK_ROOT)

    def path(self, relative: str) -> Path:
        component = Path(relative)
        if component.is_absolute() or ".." in component.parts or not component.parts:
            raise MigrationError("State paths must stay below the migration workspace")
        return local_path(local_path(self.root) / component)

    def read(self, relative: str) -> Mapping[str, Any]:
        document = read_json(self.path(relative), private=True)
        if not isinstance(document, dict):
            raise MigrationError("Migration state must be a JSON object: {}".format(relative))
        return document

    def write(self, relative: str, document: Mapping[str, Any]) -> None:
        write_private_json(self.path(relative), document, replace=True)

    @contextmanager
    def lock(self) -> Iterator[None]:
        if os.name != "posix":
            raise MigrationError("Migration locking requires the Linux Mailcow host")
        import fcntl

        private_directory(self.root)
        descriptor = os.open(str(self.path("operation.lock")), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise OperationLocked("Another preparation or migration holds the shared operation lock") from error
            yield
        finally:
            os.close(descriptor)


def require_identity(state: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
    if any(key not in state or state[key] != value for key, value in expected.items()):
        raise MigrationError("Saved migration identity differs; reconcile the existing batch before changing its connection or plan")


@dataclass(frozen=True)
class OperationJournal:
    workspace: Workspace
    relative_path: str

    def write_mutation(
        self, identifier: str, endpoint: str, payload: Any,
        call: Callable[[str, Any], Any], *, apply: bool = False,
    ) -> str:
        """Call while holding Workspace.lock, after checking the batch's saved identity."""
        state = self.workspace.read(self.relative_path)
        done = state.get("done")
        if not isinstance(done, list) or any(not isinstance(item, str) for item in done):
            raise MigrationError("Invalid operation journal: missing completed-operation list")
        if state.get("pending"):
            raise MigrationError("An uncertain operation is pending; reconcile live target state before further writes")
        if identifier in done:
            return "already completed"
        if not apply:
            return "would"
        pending = {**state, "pending": identifier, "pendingRequest": {"endpoint": endpoint, "payload": payload}}
        self.workspace.write(self.relative_path, pending)
        call(endpoint, payload)
        completed = {key: value for key, value in pending.items() if key not in ("pending", "pendingRequest")}
        self.workspace.write(self.relative_path, {**completed, "done": [*done, identifier]})
        return "completed"
