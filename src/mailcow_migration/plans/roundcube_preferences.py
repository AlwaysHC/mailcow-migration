import math
import re
from typing import Any, Callable, Mapping

from ..core.errors import MigrationError
from ..core.folders import collapsed_folders
from .roundcube_values import (
    MISSING,
    identity_flag,
    numeric_flag,
    source_json,
    source_number,
    source_string,
    source_truth,
)

DEFERRED_PREFERENCES = {
    "mdn_default": {
        "number": 413,
        "label": "Request read receipts",
        "fallback": "Read receipts are not requested automatically",
    },
    "dsn_default": {
        "number": 414,
        "label": "Request delivery notifications",
        "fallback": "Delivery notifications are not requested",
    },
    "reply_same_folder": {
        "number": 415,
        "label": "Save replies in original folder",
        "fallback": "Replies are saved in Sent",
    },
}
LANGUAGES = {
    "it_IT": "Italian",
    "en_US": "English",
    "en_GB": "English",
    "de_DE": "German",
    "fr_FR": "French",
    "es_ES": "SpanishSpain",
}
DATE_FORMATS = {"d/m/Y": "%d/%m/%Y"}
TIME_FORMATS = {"H:i": "%H:%M"}
CHOICES = {
    "date_format": ("SOGoShortDateFormat", DATE_FORMATS),
    "time_format": ("SOGoTimeFormat", TIME_FORMATS),
    "htmleditor": ("SOGoMailComposeMessageType", {"0": "text", "1": "html"}),
    "reply_mode": ("SOGoMailReplyPlacement", {"0": "below", "1": "above"}),
    "compose_extwin": (
        "SOGoMailComposeWindow",
        {"false": "inline", "true": "popup", "0": "inline", "1": "popup"},
    ),
    "forward_attachment": (
        "SOGoMailMessageForwarding",
        {"false": "inline", "true": "attached", "0": "inline", "1": "attached"},
    ),
}
SPECIAL_FOLDERS = {
    "drafts_mbox": "SOGoDraftsFolderName",
    "sent_mbox": "SOGoSentFolderName",
    "trash_mbox": "SOGoTrashFolderName",
    "junk_mbox": "SOGoJunkFolderName",
}
INTERFACE_PREFERENCES = {
    "compose_save_localstorage": "Use SOGo's draft-saving behavior; existing browser-only drafts still require recovery.",
    "emoticons_compose": "Use SOGo's native editor behavior for emoticons.",
    "emoticons_display": "Use SOGo's native message display; RoundCube's emoticon replacement plugin is not imported.",
    "search_mods": "Use SOGo's search defaults; users can choose search fields. Saved searches are checked separately.",
}
IDENTITY_FIELDS = frozenset(
    (
        "identity_id",
        "user_id",
        "changed",
        "del",
        "standard",
        "name",
        "email",
        "organization",
        "reply-to",
        "bcc",
        "signature",
        "html_signature",
    )
)


def user_preferences(
    user: Mapping[str, Any],
    defaults: Mapping[str, Any],
    validate_timezone: Callable[[str], None],
) -> Mapping[str, Any]:
    label = "{} (RoundCube user {})".format(user["login"], user["id"])
    own = user["preferences"] or {}
    source_language = next(
        (
            value
            for value in (
                user.get("language"),
                own.get("language"),
                defaults.get("language"),
            )
            if source_truth(value)
        ),
        None,
    )
    preferences = {
        "timezone": "Europe/Rome",
        **defaults,
        **own,
        "language": source_language,
    }
    mapped, blockers, native, deferred = {}, [], [], []

    def block(key: str, reason: str) -> None:
        blockers.append(f"{label}: {key}: {reason}")

    for key, value in preferences.items():
        if key in INTERFACE_PREFERENCES:
            native.append(
                f"{key}={source_json(value)}: interface preference not imported; original retained. {INTERFACE_PREFERENCES[key]}"
            )
        elif key in DEFERRED_PREFERENCES:
            feature = DEFERRED_PREFERENCES[key]
            if numeric_flag(value) and identity_flag(value) is False:
                native.append(feature["fallback"])
            elif numeric_flag(value) and identity_flag(value) is True:
                deferred.append(
                    {
                        "username": user["target"],
                        "roundcubeUser": user["id"],
                        "preference": key,
                        "value": value,
                        "pr": "https://github.com/Alinto/sogo/pull/{}".format(
                            feature["number"]
                        ),
                        "fallback": feature["fallback"],
                    }
                )
                native.append(
                    feature["fallback"]
                    + f" ({key} requires explicit deferral; original retained)"
                )
            else:
                block(key, "unrecognized boolean value " + source_json(value))
        elif key in CHOICES:
            target, values = CHOICES[key]
            if (
                type(value) in (str, int, float, bool)
                and source_string(value) in values
            ):
                mapped[target] = values[source_string(value)]
            elif key == "htmleditor" and source_number(value) in (2, 3, 4):
                block(
                    key,
                    "This mode chooses HTML/text from the original message; SOGo uses a fixed format. Explicit mapping is required",
                )
            else:
                block(
                    key,
                    f"value {source_json(value)} has no lossless SOGo mapping",
                )
        elif key == "date_long":
            parts = value.split(" ") if isinstance(value, str) else []
            if (
                len(parts) != 2
                or parts[0] not in DATE_FORMATS
                or parts[1] not in TIME_FORMATS
            ):
                block(
                    key,
                    f"value {source_json(value)} has no lossless SOGo date/time mapping",
                )
            elif (
                "time_format" in preferences and preferences["time_format"] != parts[1]
            ):
                block(
                    key,
                    "time differs from time_format; SOGo uses one time format for both views",
                )
            else:
                mapped.update(
                    SOGoLongDateFormat=DATE_FORMATS[parts[0]],
                    SOGoTimeFormat=TIME_FORMATS[parts[1]],
                )
        elif key == "mail_read_time":
            seconds = source_number(value)
            if (
                type(value) not in (int, float, str)
                or not re.fullmatch(r"-?[0-9]+", source_string(value))
                or not math.isfinite(seconds)
                or not seconds.is_integer()
                or not -1 <= seconds <= 2147483647
            ):
                block(
                    key,
                    "expected -1 (never), 0 (immediately), or a positive whole-number delay in seconds",
                )
            else:
                mapped["SOGoMailAutoMarkAsReadDelay"] = int(seconds)
        elif key == "default_list_mode":
            if value not in ("list", "threads"):
                block(key, "unrecognized default message listing mode")
            else:
                mapped["SOGoMailSortByThreads"] = int(value == "threads")
        elif key == "message_threading":
            mode = preferences.get("default_list_mode")
            if not (isinstance(value, dict) or value == []) or any(
                type(identity_flag(flag)) is not bool for flag in (value or {}).values()
            ):
                block(
                    key, "expected folder names mapped to boolean threading preferences"
                )
            elif mode not in ("list", "threads"):
                block(
                    key, "default_list_mode is missing; refresh the RoundCube inventory"
                )
            else:
                different = [
                    name
                    for name, flag in (value or {}).items()
                    if identity_flag(flag) != (mode == "threads")
                ]
                if different:
                    block(
                        key,
                        "SOGo has one threading setting; overrides differ for: "
                        + ", ".join(different),
                    )
                else:
                    native.append(
                        f"Folder threading agrees with the default {mode} view; preserve it through SOGo's global setting."
                    )
        elif key in ("message_extwin", "prefer_html"):
            flag = identity_flag(value)
            if type(flag) is bool and flag == (key == "prefer_html"):
                native.append(
                    "HTML display matches SOGo's native viewer."
                    if key == "prefer_html"
                    else "Messages open in the current SOGo window."
                )
            else:
                block(
                    key,
                    "SOGo has no reviewed mapping for this message-display behavior",
                )
        elif key in SPECIAL_FOLDERS:
            if (
                not isinstance(value, str)
                or not value
                or re.search(r"[.\x00-\x1f\x7f]|&[^-]*-", value)
            ):
                block(
                    key,
                    "special-folder hierarchy/encoding needs an explicit mapping to SOGo's slash-separated names",
                )
            else:
                mapped[SPECIAL_FOLDERS[key]] = value
        elif key == "language":
            language = LANGUAGES.get(source_string(value))
            if language:
                mapped["SOGoLanguage"] = language
            else:
                block(
                    key,
                    f"locale {source_json(value)} has no reviewed SOGo mapping",
                )
        elif key == "timezone":
            if (
                value is None
                or value == "auto"
                or isinstance(value, str)
                and not value.strip()
            ):
                mapped["SOGoTimeZone"] = "Europe/Rome"
            elif not isinstance(value, str):
                block(key, "invalid timezone value supplied by RoundCube")
            else:
                try:
                    validate_timezone(value)
                    mapped["SOGoTimeZone"] = value
                except MigrationError as error:
                    block(
                        key,
                        f"cannot validate timezone {source_json(value)}: {error}",
                    )
        elif key == "draft_autosave":
            interval = source_number(value)
            if (
                not math.isfinite(interval)
                or not interval.is_integer()
                or interval <= 0
                or interval % 60
            ):
                block(
                    key,
                    "SOGo requires positive whole minutes; disabling autosave has no verified equivalent",
                )
            else:
                mapped["SOGoMailAutoSave"] = source_string(interval / 60)
        elif key == "show_sig":
            mode = source_number(value)
            if mode not in (0, 1, 2, 3):
                block(key, "unrecognized signature insertion mode")
            else:
                mapped["SOGoMailUseSignatureOnNew"] = int(mode in (1, 2))
                mapped["SOGoMailUseSignatureOnReply"] = mapped[
                    "SOGoMailUseSignatureOnForward"
                ] = int(mode in (1, 3))
        elif key == "sig_below":
            if not numeric_flag(value):
                block(key, "unrecognized signature placement")
            else:
                mapped["SOGoMailSignaturePlacement"] = (
                    "below"
                    if (
                        source_number(preferences.get("reply_mode", MISSING)) == 0
                        or identity_flag(value) is True
                    )
                    else "above"
                )
        elif key == "sig_separator":
            if not numeric_flag(value) or identity_flag(value) is not True:
                block(
                    key,
                    "SOGo inserts a signature separator; disabling it is not mapped",
                )
        elif key == "collapsed_folders":
            try:
                collapsed_folders(value)
            except MigrationError as error:
                block(key, str(error))
        elif key == "newuserpassword":
            if identity_flag(value) is not False:
                block(
                    key,
                    "RoundCube requires a password change; map this to mailbox password policy first",
                )
            else:
                native.append(
                    "RoundCube has no pending initial password change; migrated mailbox policy applies."
                )
        elif key == "client_hash":
            if not isinstance(value, str):
                block(
                    key,
                    "unexpected internal identifier format; review the source value",
                )
        else:
            block(
                key,
                f"stored value {source_json(value)} has no reviewed SOGo mapping; it will not be discarded",
            )

    identities = []
    for identity in user["identities"]:
        if not isinstance(identity, dict):
            block("identities", "invalid source identity record")
            continue
        name = "identity " + source_string(identity.get("identity_id", MISSING))
        for key, value in identity.items():
            if (
                key not in IDENTITY_FIELDS
                or key in ("organization", "bcc")
                and source_truth(value)
            ):
                block(name + "." + key, "identity field has no lossless SOGo mapping")
        email, signature = identity.get("email"), identity.get("signature")
        if not source_truth(email):
            block(name, "sender email address is missing")
        if not isinstance(email, str) or email.lower() != user["target"].lower():
            block(
                name + ".email",
                "send-as permission must be verified before preserving this identity",
            )
        standard = source_number(identity.get("standard", MISSING))
        if standard not in (0, 1):
            block(name + ".standard", "unrecognized default identity flag")
        if (
            source_truth(signature)
            and mapped.get("SOGoMailComposeMessageType")
            and source_number(identity.get("html_signature", MISSING))
            != int(mapped["SOGoMailComposeMessageType"] == "html")
        ):
            block(
                name + ".signature",
                "signature format differs from compose format; conversion may lose formatting",
            )
        if source_truth(signature) and re.search(
            r"""(?:src\s*=\s*["']?(?!data:)[^\s"'>]+|url\s*\()""",
            source_string(signature),
            re.IGNORECASE,
        ):
            block(
                name + ".signature",
                "references external/local assets that have not been migrated",
            )
        full_name = identity.get("name")
        if full_name is not None and not isinstance(full_name, str):
            block(name + ".name", "invalid display name")
        identities.append(
            {
                "fullName": full_name
                if isinstance(full_name, str) and full_name.strip()
                else email,
                "email": email,
                "replyTo": identity.get("reply-to") or "",
                "signature": signature or "",
                "isDefault": standard == 1,
            }
        )
    default_count = sum(identity["isDefault"] for identity in identities)
    if default_count > 1:
        block(
            "identities",
            "multiple source identities are marked as default; select the intended default first",
        )
    fallback = next(
        (
            index
            for index, identity in enumerate(identities)
            if isinstance(identity["email"], str)
            and identity["email"].lower() == user["target"].lower()
        ),
        None,
    )
    if not default_count and fallback is None:
        identities.append(
            {
                "fullName": user["target"],
                "email": user["target"],
                "replyTo": "",
                "signature": "",
                "isDefault": True,
            }
        )
    mapped["SOGoMailIdentities"] = [
        {**identity, "isDefault": True}
        if not default_count and index == fallback
        else identity
        for index, identity in enumerate(identities)
    ]
    return {
        "preferences": mapped,
        "blockers": blockers,
        "nativeBehaviors": native,
        "deferredPreferences": deferred,
        "language": source_language,
    }
