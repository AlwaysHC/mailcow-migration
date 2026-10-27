import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from shutil import which
from typing import Any, Callable, Mapping, Optional, Tuple

from .configuration import Configuration, section_label
from .core.dns import MxEvidence, source_mx
from .core.domain import domain_name
from .core.errors import ConfigurationError, MigrationError
from .core.storage import local_path, write_private_json
from .sources import configured_sources
from .sources.inventory import SourceInspection, inspect_source


@dataclass(frozen=True)
class InspectionReport:
    domain: str
    checked_at: str
    sources: Tuple[SourceInspection, ...]
    mx: MxEvidence
    aliases: Tuple[MxEvidence, ...]
    matching_sources: Tuple[str, ...]
    findings: Tuple[str, ...]

    def document(self) -> Mapping[str, Any]:
        return {
            "version": 1,
            "kind": "source-inspection",
            "domain": self.domain,
            "checked_at": self.checked_at,
            "sources": [
                {
                    "kind": source.connection.kind.value,
                    "host": source.connection.ssh.host,
                    "inventory": asdict(source.inventory),
                    "ips": source.ips,
                    "domain_directory": source.domain_directory,
                    "missing_maildirs": source.missing_maildirs,
                }
                for source in self.sources
            ],
            "mx": asdict(self.mx),
            "aliases": tuple(asdict(alias) for alias in self.aliases),
            "matching_sources": self.matching_sources,
            "findings": self.findings,
        }


def matches_source(
    source: SourceInspection,
    others: Tuple[SourceInspection, ...],
    mx: MxEvidence,
    target: Tuple[str, ...] = (),
) -> bool:
    return any(
        all(address in (*source.ips, *target) for address in record.ips)
        for record in mx.records
    ) and all(address not in other.ips for address in mx.ips for other in others)


def inspect_domain(
    configuration: Configuration,
    domain: str,
    tell: Callable[[str], None] = print,
    *,
    target_addresses: Optional[Mapping[str, Tuple[str, ...]]] = None,
) -> InspectionReport:
    domain_name(domain)
    sources = []
    for connection in configured_sources(configuration):
        label = section_label(connection.kind.value)
        tell(f"Inspecting {label} at {connection.ssh.host} (read-only)...")
        source = inspect_source(connection, domain)
        inventory = source.inventory
        status = (
            "enabled"
            if inventory.active
            else "inactive"
            if inventory.present
            else "no primary mail domain"
        )
        tell(
            "  {}; {} mailboxes; {} missing Maildirs; domain directory {}.".format(
                status,
                len(inventory.mailboxes),
                len(source.missing_maildirs),
                "present" if source.domain_directory else "absent",
            )
        )
        tell("  Source addresses: {}".format(", ".join(source.ips)))
        sources.append(source)
    tell(f"Checking authoritative MX and mail-host addresses for {domain}...")
    mx = source_mx(domain)
    tell(
        "MX destinations: "
        + "; ".join(
            "{} {} ({})".format(record.priority, record.exchange, ", ".join(record.ips))
            for record in mx.records
        )
    )
    targets = target_addresses or {}
    matches = tuple(
        source
        for source in sources
        if matches_source(
            source,
            tuple(other for other in sources if other is not source),
            mx,
            targets.get(source.connection.kind.value, ()),
        )
    )
    findings = []
    aliases = []
    if len(matches) != 1:
        findings.append(
            "MX records do not identify exactly one configured source; check unknown, multiple or shared addresses."
        )
    else:
        source = matches[0]
        label = section_label(source.connection.kind.value)
        inventory = source.inventory
        if inventory.primary != domain:
            findings.append(
                f"{domain} is a {label} mail alias; inspect the primary domain {inventory.primary}."
            )
        if not inventory.present or not inventory.active or source.missing_maildirs:
            findings.append(
                f"DNS points to {label}, but its enabled mail configuration or Maildir inventory does not agree."
            )
        if source.missing_maildirs:
            findings.append(
                "Missing or unsafe Maildirs: {}.".format(
                    ", ".join(source.missing_maildirs)
                )
            )
        others = tuple(other for other in sources if other is not source)
        for alias in inventory.aliases:
            tell(f"Checking mail alias {alias}...")
            evidence = source_mx(alias)
            aliases.append(evidence)
            if not matches_source(
                source, others, evidence, targets.get(source.connection.kind.value, ())
            ):
                findings.append(
                    f"Mail alias {alias} does not route to the same {label} source or its recorded activated target."
                )
    return InspectionReport(
        domain,
        datetime.now(timezone.utc).isoformat(),
        tuple(sources),
        mx,
        tuple(aliases),
        tuple(source.connection.kind.value for source in matches),
        tuple(findings),
    )


def require_inspection_host(additional_commands: Tuple[str, ...] = ()) -> None:
    if sys.platform != "linux":
        raise ConfigurationError("Run source inspection on the Linux Mailcow host")
    missing = tuple(
        command
        for command in ("ssh", "dig", *additional_commands)
        if which(command) is None
    )
    if missing:
        raise MigrationError(
            "Source inspection requires these local commands: {}".format(
                ", ".join(missing)
            )
        )


def run_inspection(
    configuration: Configuration, domain: str, output: Optional[Path] = None
) -> int:
    domain_name(domain)
    require_inspection_host()
    if output is not None and local_path(output).exists():
        raise ConfigurationError(
            "Inspection report already exists; choose another --output path"
        )
    report = inspect_domain(configuration, domain)
    if output is not None:
        write_private_json(output, report.document())
        print(f"Private inspection report saved to {output}")
    for finding in report.findings:
        print("Finding: " + finding)
    if not report.findings:
        print(
            f"Source inventory and DNS agree on {section_label(report.matching_sources[0])}."
        )
    print(
        "Inspection does not establish migration readiness or validate an existing batch for resume."
    )
    return 1 if report.findings else 0
