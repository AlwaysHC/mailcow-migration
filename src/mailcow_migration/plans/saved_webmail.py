from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from .evidence import ExportEvidence
from .forwarding import mailbox_forwardings
from .policy import load_policy
from .saved import SavedPlan
from .sieve_evidence import load_sieve_evidence
from .webmail import webmail_assessment
from ..core.errors import MigrationError
from ..core.storage import json_fingerprint, read_json


@dataclass(frozen=True)
class SavedWebmail:
    proposal: Mapping[str, Any] = field(repr=False)
    policy: Mapping[str, Any] = field(repr=False)
    forwarding: Mapping[str, Any] = field(repr=False)

    @property
    def blockers(self) -> list:
        return [row["id"] + ": " + row["detail"] for row in (*self.forwarding["checks"], *self.proposal["checks"])
                if row["status"] in ("blocked", "decision")]


def load_saved_webmail(batch: SavedPlan, evidence: ExportEvidence) -> SavedWebmail:
    saved = read_json(batch.directory / "webmail-plan.json", private=True)
    if (not isinstance(saved, dict) or saved.get("version") != 1 or saved.get("kind") != "source-webmail-plan"
            or saved.get("target_verified") is not False or not isinstance(saved.get("checked_at"), str)):
        raise MigrationError("Missing or invalid saved webmail proposal; retain the planning evidence")
    try:
        planned_at = datetime.fromisoformat(saved["checked_at"])
    except ValueError as error:
        raise MigrationError("Invalid saved webmail planning time") from error
    if planned_at.tzinfo is None:
        raise MigrationError("Saved webmail planning time must include its timezone")
    policy = load_policy(str(batch.directory / "policy.json"))
    forwarding = mailbox_forwardings(batch.plan)
    scripts = load_sieve_evidence(batch.plan, evidence)
    planned = webmail_assessment(batch.plan, scripts, policy, batch.source.kind, forwarding["forwardings"], now=planned_at)
    if json_fingerprint(saved, canonical=True) != json_fingerprint(planned, canonical=True):
        raise MigrationError("Saved webmail proposal differs from its export or policy; reconcile it before changing filters or settings")
    return SavedWebmail(planned, policy, forwarding)
