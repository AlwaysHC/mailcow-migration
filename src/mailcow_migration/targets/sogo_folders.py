import re
from pathlib import Path
from typing import Any, Mapping, Optional
from uuid import uuid4

from .mailcow import MailcowTarget
from .sogo_values import equal_values
from ..core.errors import MigrationError
from ..core.folders import folder_inventory
from ..core.storage import decode_json, json_fingerprint, write_private_json


CSS_ESCAPES = str.maketrans({"_": "_U_", ".": "_D_", "#": "_H_", "@": "_A_", "*": "_S_", ":": "_C_", ",": "_CO_",
                            " ": "_SP_", "'": "_SQ_", "&": "_AM_", "+": "_P_"})


def folder_id(folder: str) -> str:
    return "/0/" + "/".join("folder" + re.sub(r"^(?=[0-9])", "_", part.translate(CSS_ESCAPES)) for part in folder.split("/"))


def primary_folder(identifier: str) -> bool:
    return identifier == "/0" or identifier.startswith("/0/")


def expanded_folders(value: Any) -> list:
    try:
        folders = decode_json(value) if isinstance(value, str) else value
    except (ValueError, UnicodeError) as error:
        raise MigrationError("SOGo Mail.ExpandedFolders contains invalid JSON") from error
    if not isinstance(folders, list) or any(not isinstance(item, str) or not re.match(r"^/[0-9]+(?:/|$)", item) for item in folders):
        raise MigrationError("SOGo Mail.ExpandedFolders is not a supported folder identifier list")
    return folders


def mail_settings(backup: Mapping[str, Any], username: str) -> Mapping[str, Any]:
    preferences = backup.get("preferences")
    settings = preferences[1] if isinstance(preferences, list) and len(preferences) > 1 else None
    if not isinstance(settings, dict) or "Mail" in settings and not isinstance(settings["Mail"], dict):
        raise MigrationError(username + ": SOGo backup has no valid user-settings dictionary")
    return settings.get("Mail", {})


def inspect_folder_state(
    target: MailcowTarget, directory: Path, user: Mapping[str, Any], backup: Mapping[str, Any],
    journal: Optional[Mapping[str, Any]] = None,
) -> Optional[Mapping[str, Any]]:
    plan = user["folderState"]
    if plan is None:
        return None
    username = user["username"]
    inventory = folder_inventory(lambda arguments: target.compose(
        ("exec", "-T", "dovecot-mailcow", *arguments),
        diagnostic_file=directory / ("folder-diagnostics-" + uuid4().hex + ".txt"),
    ).stdout, username)
    missing = [folder for folder in plan["source"]["folders"] if folder not in inventory["folders"]]
    expected = sorted({"/0", *(folder_id(folder) for folder in inventory["folders"] if folder not in plan["collapsed"])})
    evidence = {"username": username, "source": plan["source"], "collapsed": plan["collapsed"], "target": inventory, "expected": expected}
    conflict = directory / ("folder-state-conflict-" + json_fingerprint(username) + ".json")
    if missing:
        write_private_json(conflict, {**evidence, "missing": missing}, replace=True)
        raise MigrationError(username + ": source folders are missing on Mailcow: " + ", ".join(missing)
                             + ". Finish copying mail before folder-state inspection. Private comparison: " + str(conflict))
    if journal is not None and (journal["source"] != plan["source"] or journal["expected"] != expected):
        raise MigrationError(username + ": folder tree changed after import started; retain the import journal")
    mail = mail_settings(backup, username)
    present = "ExpandedFolders" in mail
    expanded = expanded_folders(mail["ExpandedFolders"]) if present else []
    actual = sorted({identifier for identifier in expanded if primary_folder(identifier)})
    if (present and actual != expected) or (not present and journal is not None and journal["complete"]):
        write_private_json(conflict, {**evidence, "actual": actual, "present": present}, replace=True)
        raise MigrationError(username + ": existing SOGo folder expansion differs from the proposal; no overwrite performed. "
                             "Private comparison: " + str(conflict))
    if journal is not None:
        normalized = {**mail, "ExpandedFolders": sorted(set(expanded)) if present else journal["mail"]["ExpandedFolders"]}
        if not equal_values(normalized, journal["mail"]):
            raise MigrationError(username + ": SOGo Mail settings changed during folder-state import; retain the import journal")
    proposed = {**mail, "ExpandedFolders": sorted({*expected, *(identifier for identifier in expanded if not primary_folder(identifier))})}
    return {**evidence, "current_mail": mail, "proposed_mail": proposed, "write_required": not present,
            "target_verified": True, "import_complete": False}
