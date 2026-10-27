import re
from dataclasses import dataclass, replace
from typing import Any, Mapping, Optional, Sequence, Tuple

from .domain import active
from .evidence import ExportFiles, optional_export_text

MAP_NAMES = ("transport", "sender_access", "rbl_override")


@dataclass(frozen=True)
class PostfixEvidence:
    postfix: Optional[str]
    services: Optional[str]
    dovecot: Optional[str]
    maps: Mapping[str, Optional[str]]
    administrators: Tuple[Mapping[str, Any], ...]


def load_postfix_evidence(
    plan: Mapping[str, Any], evidence: ExportFiles
) -> PostfixEvidence:
    return postfix_evidence(evidence, plan["source"]["admins"])


def postfix_evidence(
    evidence: ExportFiles, administrators: Sequence[Mapping[str, Any]]
) -> PostfixEvidence:
    return PostfixEvidence(
        optional_export_text(evidence, "postfix-effective.conf"),
        optional_export_text(evidence, "postfix-services.conf"),
        optional_export_text(evidence, "dovecot-effective.conf"),
        {name: optional_export_text(evidence, "config", name) for name in MAP_NAMES},
        tuple(
            {"username": row["username"], "active": row["active"]}
            for row in administrators
            if active(row.get("superadmin"))
        ),
    )


def source_policy_evidence(evidence: PostfixEvidence) -> PostfixEvidence:
    if evidence.postfix is None:
        return evidence
    postfix = re.sub(
        r"^(transport_maps\s*=\s*)hash:/etc/postfix/mailcow-migration-transport,\s*",
        r"\1",
        evidence.postfix,
        count=1,
        flags=re.MULTILINE,
    )
    postfix = re.sub(
        r"^(virtual_alias_maps\s*=\s*)regexp:/etc/postfix/mailcow-migration-aliases,\s*",
        r"\1",
        postfix,
        count=1,
        flags=re.MULTILINE,
    )
    postfix = re.sub(
        r"^(sender_bcc_maps|recipient_bcc_maps)\s*=.*$",
        lambda match: re.sub(
            r"(hash:/etc/postfix/[a-zA-Z0-9_./-]+)\.mailcow-migration(?=[,\s]|$)",
            r"\1",
            match[0],
        ),
        postfix,
        flags=re.MULTILINE,
    )
    return replace(evidence, postfix=postfix)
