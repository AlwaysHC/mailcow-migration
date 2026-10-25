import re
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import unquote

from .errors import MigrationError
from .storage import decode_json


def collapsed_folders(value: Any) -> list:
    if not isinstance(value, str) or not re.fullmatch(r"(?:&[^&]+&)*", value):
        raise MigrationError("collapsed_folders must be empty or an ampersand-delimited list of encoded folder names")
    encoded = re.findall(r"&([^&]+)&", value)
    if any(re.search(r"%(?![0-9a-fA-F]{2})", name) for name in encoded):
        raise MigrationError("Invalid percent-encoding in collapsed_folders")
    try:
        return sorted({unquote(name, encoding="utf-8", errors="strict") for name in encoded})
    except UnicodeError as error:
        raise MigrationError("Invalid UTF-8 encoding in collapsed_folders") from error


def folder_path(name: Any, separator: str) -> str:
    if not isinstance(name, str) or not name or re.search(r"[^\x20-\x7e]", name):
        raise MigrationError("Expected a nonempty IMAP modified UTF-7 folder name without control characters")
    parts = name.split(separator)
    if any(not part or part in (".", "..") or "/" in part for part in parts):
        raise MigrationError("Folder has an unsupported hierarchy: " + name)
    return "/".join(parts)


def default_separator(run: Callable[[Sequence[str]], str]) -> str:
    version = re.match(r"2\.([0-9]+)\.", run(("dovecot", "--version")).strip())
    if not version:
        raise MigrationError("Folder-state migration requires a reviewed Dovecot 2.x configuration")
    if int(version[1]) >= 4:
        layout = run(("doveconf", "-h", "mailbox_list_layout")).strip().lower()
        driver = run(("doveconf", "-h", "mail_driver")).strip()
        if layout == "fs":
            return "/"
        if layout == "maildir++" or not layout and driver == "maildir":
            return "."
    else:
        location = run(("doveconf", "-h", "mail_location")).strip()
        match = re.search(r":LAYOUT=([^:]+)", location)
        layout = match[1].lower() if match else "maildir++"
        if location.startswith("maildir:") and layout in ("fs", "maildir++"):
            return "/" if layout == "fs" else "."
    raise MigrationError("Cannot establish the default folder separator from the Dovecot storage layout")


def folder_inventory(run: Callable[[Sequence[str]], str], username: str) -> Mapping[str, Any]:
    if not re.fullmatch(r"[a-zA-Z0-9_.+%-]+@[a-zA-Z0-9.-]+", username):
        raise MigrationError("Folder inspection requires a literal mailbox username")
    if run(("doveconf", "-h", "namespace/inbox/prefix")).strip():
        raise MigrationError(username + ": folder-state migration requires an empty personal namespace prefix")
    separator = run(("doveconf", "-h", "namespace/inbox/separator")).strip() or default_separator(run)
    if separator not in (".", "/"):
        raise MigrationError(username + ": unsupported personal folder hierarchy separator")
    try:
        rows = decode_json(run(("doveadm", "-f", "json", "mailbox", "list", "-7", "-u", username)))
    except (ValueError, UnicodeError) as error:
        raise MigrationError(username + ": invalid Dovecot folder inventory") from error
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise MigrationError(username + ": invalid Dovecot folder inventory")
    paths = (folder_path(row.get("mailbox"), separator).split("/") for row in rows)
    folders = sorted({"/".join(parts[:index + 1]) for parts in paths for index in range(len(parts))})
    if "INBOX" not in folders:
        raise MigrationError(username + ": Dovecot folder inventory has no INBOX")
    return {"separator": separator, "folders": folders}
