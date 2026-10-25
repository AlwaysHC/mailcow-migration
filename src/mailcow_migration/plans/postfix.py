import re
from dataclasses import dataclass, replace
from typing import Any, Mapping, Optional, Tuple

from .domain import active
from .evidence import ExportEvidence, optional_export_text

MAP_NAMES = ("transport", "sender_access", "rbl_override")


@dataclass(frozen=True)
class PostfixEvidence:
    postfix: Optional[str]
    services: Optional[str]
    dovecot: Optional[str]
    maps: Mapping[str, Optional[str]]
    administrators: Tuple[Mapping[str, Any], ...]


def load_postfix_evidence(plan: Mapping[str, Any], evidence: ExportEvidence) -> PostfixEvidence:
    return PostfixEvidence(
        optional_export_text(evidence, "postfix-effective.conf"), optional_export_text(evidence, "postfix-services.conf"),
        optional_export_text(evidence, "dovecot-effective.conf"),
        {name: optional_export_text(evidence, "config", name) for name in MAP_NAMES},
        tuple({"username": row["username"], "active": row["active"]} for row in plan["source"]["admins"] if active(row.get("superadmin"))),
    )


def source_policy_evidence(evidence: PostfixEvidence) -> PostfixEvidence:
    if evidence.postfix is None:
        return evidence
    postfix = re.sub(r"^(transport_maps\s*=\s*)hash:/etc/postfix/mailcow-migration-transport,\s*", r"\1",
                     evidence.postfix, count=1, flags=re.M)
    postfix = re.sub(r"^(virtual_alias_maps\s*=\s*)regexp:/etc/postfix/mailcow-migration-aliases,\s*", r"\1",
                     postfix, count=1, flags=re.M)
    postfix = re.sub(r"^(sender_bcc_maps|recipient_bcc_maps)\s*=.*$", lambda match: re.sub(
        r"(hash:/etc/postfix/[a-zA-Z0-9_./-]+)\.mailcow-migration(?=[,\s]|$)", r"\1", match[0]), postfix, flags=re.M)
    return replace(evidence, postfix=postfix)


def logical_lines(text: Optional[str]) -> Tuple[str, ...]:
    lines = [line for line in re.split(r"\r?\n", text or "") if line.strip() and not line.lstrip().startswith("#")]
    return tuple(re.sub(r"\n[ \t]+", " ", "\n".join(lines)).split("\n"))


def postfix_settings(text: Optional[str]) -> Mapping[str, str]:
    unfolded = re.sub(r"\n[ \t]+", " ", (text or "").replace("\r", ""))
    return {match[1]: match[2] for line in unfolded.split("\n")
            if (match := re.fullmatch(r"([a-z][a-z0-9_]*)\s*=\s*(.*?)\s*", line)) is not None}
