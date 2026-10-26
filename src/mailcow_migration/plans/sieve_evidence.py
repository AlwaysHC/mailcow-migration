from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple

from .evidence import ExportEvidence, optional_export_text
from .forwarding import sieve_string
from ..core.errors import MigrationError
from ..core.storage import read_json
from ..sources import SourceKind


@dataclass(frozen=True)
class SieveEvidence:
    scripts: Mapping[str, Optional[str]]
    default_sieve: Optional[str]
    inactive_sieve: Optional[Tuple[str, ...]]
    dates_utc: bool
    forwardings: Tuple[Mapping[str, Any], ...] = ()

    def script(self, username: str) -> Optional[str]:
        personal = self.scripts.get(username)
        return self.default_sieve if personal is None else personal


def sieve_inventory_blockers(evidence: SieveEvidence) -> list:
    return (["Inactive Sieve inventory is missing; refresh the source export"] if evidence.inactive_sieve is None else
            ["Inactive Sieve script has no editable SOGo mapping: " + file for file in evidence.inactive_sieve])


def load_sieve_evidence(plan: Mapping[str, Any], evidence: ExportEvidence) -> SieveEvidence:
    if evidence.source.connection.kind is SourceKind.POSTFIXADMIN:
        scripts = {row["username"]: optional_export_text(evidence, "sieve", plan["domain"], row["payload"]["local_part"], ".dovecot.sieve")
                   for row in plan["mailboxes"]}
        inactive = optional_export_text(evidence, "config", "inactive-sieve.files")
        return SieveEvidence(scripts, optional_export_text(evidence, "config", "default.sieve"),
                             tuple(file for file in inactive.split("\0") if file) if inactive is not None else None,
                             bool(plan["source"].get("vacationDatesUtc")))
    snapshot = read_json(evidence.data / "source.json", private=True)
    scripts, forwardings = {}, []
    for row in snapshot["accounts"]:
        if not row["mailbox"]:
            continue
        script = row["script"]
        if row["forwarding"]:
            if script:
                raise MigrationError(row["username"] + ": forwarding with personal Sieve requires an explicit ordering adapter")
            targets = [target.lower() for target in row["destinations"]]
            script = ('require ["copy"];\n' if row["delivery"] else "") + "\n".join(
                "redirect{} {};".format(" :copy" if row["delivery"] else "", sieve_string(target)) for target in targets
            ) + ("\n" if row["delivery"] else "\nstop;\n")
            forwardings.append({"username": row["username"], "destinations": targets, "active": 1,
                                "keepLocal": row["delivery"], "method": "personal-sieve"})
        if script is not None and not isinstance(script, str):
            raise MigrationError("Invalid Plesk personal Sieve evidence")
        scripts[row["username"]] = script if script else None
    return SieveEvidence(scripts, None, (), False, tuple(forwardings))
