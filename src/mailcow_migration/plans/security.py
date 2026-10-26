from typing import Any, Mapping

from .recovery import password_expiration_check
from .saved import SavedPlan
from .settings_policy import load_policy
from ..core.errors import MigrationError
from ..core.storage import json_fingerprint


def account_security_policy(batch: SavedPlan, state: Mapping[str, Any]) -> str:
    policy = load_policy(str(batch.directory / "policy.json"))
    expiration = password_expiration_check(policy)
    reviewed = tuple(row for row in batch.assessment["checks"] if row["id"] == "password-expiration")
    if reviewed != (expiration,):
        raise MigrationError("Password-expiration policy differs from its saved assessment; retain the batch and reconcile")
    if expiration["status"] == "blocked":
        raise MigrationError(expiration["detail"])
    fingerprint = json_fingerprint(policy, canonical=True)
    if "policy_sha256" in state and state["policy_sha256"] != fingerprint:
        raise MigrationError("Settings policy changed after account operations started; retain the original policy and journal")
    return fingerprint
