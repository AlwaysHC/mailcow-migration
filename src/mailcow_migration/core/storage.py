import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple

from .errors import MigrationError


def local_path(path: Path) -> Path:
    absolute = Path(os.path.abspath(path.expanduser()))
    for item in (absolute, *absolute.parents):
        if item.is_symlink():
            raise MigrationError("Symlinked configuration or state path: {}".format(item))
    return absolute


def private_directory(path: Path) -> Path:
    directory = local_path(path)
    for item in reversed((directory, *directory.parents)):
        if not item.exists():
            item.mkdir(mode=0o700)
        elif not item.is_dir():
            raise MigrationError("Expected a directory: {}".format(item))
    return directory


def read_text(path: Path, *, private: bool = False) -> str:
    file = local_path(path)
    metadata = file.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise MigrationError("Expected a regular file: {}".format(file))
    if private and os.name == "posix" and metadata.st_mode & 0o077:
        raise MigrationError("File must have owner-only permissions: {} (use chmod 600)".format(file))
    return file.read_text(encoding="utf-8")


def unique_object(pairs: Iterable[Tuple[str, Any]]) -> Dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def invalid_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def decode_json(content: str) -> Any:
    return json.loads(content, object_pairs_hook=unique_object, parse_constant=invalid_constant)


def read_json(path: Path, *, private: bool = False) -> Any:
    try:
        return decode_json(read_text(path, private=private))
    except (ValueError, UnicodeError) as error:
        raise MigrationError("Invalid UTF-8 JSON in {}; contents omitted".format(path)) from error


def write_private_json(path: Path, value: Any, *, replace: bool = False) -> None:
    write_private_text(path, json.dumps(value, ensure_ascii=True, indent=2, allow_nan=False) + "\n", replace=replace)


def write_private_text(path: Path, content: str, *, replace: bool = False) -> None:
    if os.name != "posix":
        raise MigrationError("Private configuration and state writes require a POSIX host; run on the Linux Mailcow host")
    destination = local_path(path)
    parent = private_directory(destination.parent)
    if destination.exists() and (not replace or not destination.is_file()):
        raise MigrationError("File already exists; choose another output path: {}".format(destination))
    descriptor, name = tempfile.mkstemp(prefix=".{}-".format(destination.name), dir=str(parent))
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        local_path(destination)
        if replace:
            os.replace(str(temporary), str(destination))
        else:
            os.link(str(temporary), str(destination))
        directory_descriptor = os.open(str(parent), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()
