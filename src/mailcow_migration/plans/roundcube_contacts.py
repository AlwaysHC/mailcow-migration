import re
from typing import Any, Mapping

from ..core.errors import MigrationError
from ..core.storage import json_fingerprint
from .roundcube_values import MISSING, source_json, source_string, source_truth


def normalized_card(value: str) -> str:
    return re.sub(
        r"\n[ \t]", "", value.replace("\r\n", "\n").replace("\r", "\n")
    ).strip()


def escape_card(value: Any) -> str:
    text = source_string(value if source_truth(value) else "").replace("\\", "\\\\")
    return re.sub(r"\r\n|\r|\n", r"\\n", text).replace(";", "\\;").replace(",", "\\,")


def contact_record(
    row: Mapping[str, Any], identity: Mapping[str, Any]
) -> Mapping[str, str]:
    identifier = source_string(row.get("contact_id", MISSING))
    uid = "rc-" + json_fingerprint({"identity": identity, "id": identifier})
    vcard = row.get("vcard")
    if vcard is not None and not isinstance(vcard, str):
        raise MigrationError("RoundCube contact " + identifier + ": invalid vCard text")
    if vcard and vcard.strip():
        lines = normalized_card(vcard).split("\n")
        if (
            lines[0].upper() != "BEGIN:VCARD"
            or lines[-1].upper() != "END:VCARD"
            or sum(line.upper() == "BEGIN:VCARD" for line in lines) != 1
            or not any(line.upper() == "VERSION:3.0" for line in lines)
            or not any(re.match(r"FN[;:]", line, re.IGNORECASE) for line in lines)
        ):
            raise MigrationError(
                "RoundCube contact "
                + identifier
                + ": expected one vCard 3.0 with a display name"
            )
        lines = lines[1:-1]
    else:
        emails = [
            email.strip()
            for email in source_string(row.get("email") or "").split(",")
            if email.strip()
        ]
        name = row.get("name") or " ".join(
            source_string(row[key])
            for key in ("firstname", "surname")
            if source_truth(row.get(key))
        )
        name = name or (emails[0] if emails else None)
        if not name:
            raise MigrationError(
                "RoundCube contact "
                + identifier
                + ": no vCard, name or email to import"
            )
        lines = [
            "VERSION:3.0",
            "FN:" + escape_card(name),
            "N:"
            + escape_card(row.get("surname"))
            + ";"
            + escape_card(row.get("firstname"))
            + ";;;",
            *("EMAIL:" + escape_card(email) for email in emails),
        ]
    identifiers = [line for line in lines if re.match(r"UID[;:]", line, re.IGNORECASE)]
    if len(identifiers) > 1:
        raise MigrationError(
            "RoundCube contact " + identifier + ": multiple UIDs need reconciliation"
        )
    if any(
        re.match(
            r"(?:PHOTO|LOGO|SOUND|KEY)(?:;[^:]*)?:(?:https?:|file:|/)",
            line,
            re.IGNORECASE,
        )
        for line in lines
    ):
        raise MigrationError(
            "RoundCube contact "
            + identifier
            + ": linked assets must be embedded or migrated before preserving this card"
        )
    content = (
        "\r\n".join(
            [
                "BEGIN:VCARD",
                *lines,
                *([] if identifiers else ["UID:" + uid]),
                "END:VCARD",
            ]
        )
        + "\r\n"
    )
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", content):
        raise MigrationError(
            "RoundCube contact " + identifier + ": unsupported control characters"
        )
    return {"c_name": uid + ".vcf", "c_content": content}


def collected_contact_record(
    row: Mapping[str, Any], identity: Mapping[str, Any]
) -> Mapping[str, str]:
    label = "RoundCube collected address {} ({})".format(
        row.get("address_id"), source_json(row.get("email"))
    )
    kind = source_string(row.get("type", MISSING))
    if kind == "2":
        raise MigrationError(
            label
            + ": trusted-sender image-loading permission has no SOGo conversion; source retained"
        )
    if kind != "1":
        raise MigrationError(
            label
            + ": unsupported address type "
            + source_json(row.get("type", MISSING))
        )
    unknown = set(row) - {"address_id", "changed", "name", "email", "user_id", "type"}
    if unknown:
        raise MigrationError(label + ": unmapped fields: " + ", ".join(sorted(unknown)))
    if not re.fullmatch(
        r"[0-9]+", source_string(row.get("address_id", MISSING))
    ) or source_string(row.get("user_id")) != source_string(identity["user"]):
        raise MigrationError(label + ": invalid record ID or owner")
    if (
        not isinstance(row.get("email"), str)
        or not re.fullmatch(r"[^\s@,<>]+@[^\s@,<>]+", row["email"])
        or not isinstance(row.get("name"), str)
    ):
        raise MigrationError(
            label + ": expected one email address and a text display name"
        )
    if not isinstance(row.get("changed"), str):
        raise MigrationError(label + ": invalid modification timestamp")
    vcard = "\r\n".join(
        [
            "BEGIN:VCARD",
            "VERSION:3.0",
            "FN:" + escape_card(row["name"] or row["email"]),
            "N:;;;;",
            "EMAIL:" + escape_card(row["email"]),
            "CATEGORIES:Roundcube collected recipients",
            "X-ROUNDCUBE-CHANGED:" + escape_card(row["changed"]),
            "END:VCARD",
        ]
    )
    return contact_record(
        {"contact_id": row["address_id"], "vcard": vcard},
        {**identity, "table": "collected_addresses"},
    )
