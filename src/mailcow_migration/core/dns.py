import re
import shlex
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Optional, Tuple

from .commands import run_command
from .domain import domain_name
from .errors import ConfigurationError, MigrationError


@dataclass(frozen=True)
class DnsRecord:
    owner: str
    kind: str
    values: Tuple[str, ...]
    ttl: int = 0


@dataclass(frozen=True)
class MxRecord:
    priority: int
    exchange: str
    ips: Tuple[str, ...]


@dataclass(frozen=True)
class MxEvidence:
    domain: str
    records: Tuple[MxRecord, ...]

    @property
    def ips(self) -> Tuple[str, ...]:
        return tuple(sorted({address for record in self.records for address in record.ips}))


def normalized_ip(value: str, version: Optional[int] = None) -> str:
    try:
        address = ip_address(value)
    except ValueError as error:
        raise MigrationError("Invalid IP address in source or DNS inventory") from error
    if "%" in value or version is not None and address.version != version:
        raise MigrationError("Unexpected IP address family in source or DNS inventory")
    return str(getattr(address, "ipv4_mapped", None) or address)


def hostname(value: str) -> str:
    try:
        return domain_name(value[:-1].lower() if value.endswith(".") else value.lower())
    except ConfigurationError as error:
        raise MigrationError("DNS response contains an invalid hostname; explicit mail hosts are required") from error


def record_name(value: str) -> str:
    if (not isinstance(value, str) or len(value) > 253 or "." not in value
            or any(not re.fullmatch(r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?", label) for label in value.split("."))):
        raise ConfigurationError("Use a lowercase fully qualified DNS record name")
    return value


def query(name: str, kind: str, server: Optional[str] = None, *, allow_missing: bool = False) -> Tuple[DnsRecord, ...]:
    record_name(name)
    if kind not in ("NS", "MX", "A", "AAAA", "TXT", "CNAME"):
        raise MigrationError("Unsupported DNS query type")
    destination = ("@" + domain_name(server), "+norecurse") if server else ()
    output = run_command(
        ("dig", *destination, name, kind, "+noall", "+answer", "+comments", "+time=5", "+tries=1"),
        timeout=30, label="DNS {} query for {}".format(kind, name), private_output=False,
    ).stdout
    if server and not re.search(r"flags: [^;]*\baa\b", output):
        raise MigrationError("DNS answer is not authoritative for {} at {}".format(name, server))
    if allow_missing and re.search(r"status: NXDOMAIN\b", output):
        return ()
    if not re.search(r"status: NOERROR\b", output):
        raise MigrationError("DNS {} query failed for {} at {}".format(kind, name, server or "the configured resolver"))
    records = []
    for line in output.splitlines():
        if not line.strip() or line.lstrip().startswith(";"):
            continue
        parts = line.split()
        if len(parts) < 5 or not re.fullmatch(r"[0-9]+", parts[1]) or parts[2] != "IN":
            raise MigrationError("Cannot interpret DNS answer for {}".format(name))
        owner = parts[0].lower().rstrip(".")
        if owner == name and parts[3] in (kind, "CNAME"):
            records.append(DnsRecord(owner, parts[3], tuple(parts[4:]), int(parts[1])))
    return tuple(records)


def nameservers(name: str) -> Tuple[str, ...]:
    zone = record_name(name)
    while "." in zone:
        records = query(zone, "NS", allow_missing=True)
        if records:
            if any(record.kind != "NS" or len(record.values) != 1 for record in records):
                raise MigrationError("Invalid nameserver inventory for {}".format(zone))
            return tuple(sorted({hostname(record.values[0]) for record in records}))
        zone = zone.split(".", 1)[1]
    raise MigrationError("No authoritative nameservers found for {}".format(name))


def agreed_records(name: str, kind: str, *, allow_missing: bool = False) -> Tuple[DnsRecord, ...]:
    answers = tuple(query(name, kind, server, allow_missing=allow_missing) for server in nameservers(name))
    signatures = {
        tuple(sorted((record.kind, tuple(value if record.kind == "TXT" else value.lower() for value in record.values)) for record in answer))
        for answer in answers
    }
    if len(signatures) != 1:
        raise MigrationError("Authoritative nameservers disagree on {} for {}; wait for DNS to agree".format(kind, name))
    return answers[0]


def cname_target(name: str) -> Optional[str]:
    records = agreed_records(record_name(name), "CNAME", allow_missing=True)
    if not records:
        return None
    if len(records) != 1 or records[0].kind != "CNAME" or len(records[0].values) != 1:
        raise MigrationError("Expected at most one CNAME at " + name)
    return record_name(records[0].values[0].lower().rstrip("."))


def text_records(name: str, *, follow_aliases: bool = True) -> Tuple[str, ...]:
    seen = set()
    for _ in range(16):
        name = record_name(name)
        if name in seen:
            raise MigrationError("DNS CNAME loop while reading TXT records")
        seen.add(name)
        records = agreed_records(name, "TXT", allow_missing=True)
        aliases = tuple(row for row in records if row.kind == "CNAME")
        if aliases:
            if not follow_aliases or len(records) != 1 or len(aliases[0].values) != 1:
                raise MigrationError("Ambiguous or unsupported TXT alias at " + name)
            name = record_name(aliases[0].values[0].lower().rstrip("."))
            continue
        try:
            return tuple("".join(shlex.split(" ".join(row.values))) for row in records)
        except ValueError as error:
            raise MigrationError("Invalid quoted DNS TXT record at " + name) from error
    raise MigrationError("DNS TXT alias chain exceeds the supported depth")


def authoritative_addresses(name: str) -> Tuple[str, ...]:
    records = (*agreed_records(name, "A"), *agreed_records(name, "AAAA"))
    if any(record.kind == "CNAME" for record in records):
        raise MigrationError("MX destination {} is a CNAME; source detection is ambiguous".format(name))
    if not records or any(len(record.values) != 1 for record in records):
        raise MigrationError("MX destination {} has no valid authoritative A/AAAA addresses".format(name))
    return tuple(sorted({normalized_ip(record.values[0], 4 if record.kind == "A" else 6) for record in records}))


def source_mx(domain: str) -> MxEvidence:
    records = agreed_records(domain, "MX")
    if not records or any(
        record.kind != "MX" or len(record.values) != 2 or not re.fullmatch(r"[0-9]+", record.values[0])
        for record in records
    ):
        raise MigrationError("{} requires explicit MX records with valid mail hosts; no source was guessed".format(domain))
    destinations = tuple((int(record.values[0]), hostname(record.values[1])) for record in records)
    if any(priority > 65535 for priority, _ in destinations):
        raise MigrationError("Invalid MX priority for {}".format(domain))
    addresses = {exchange: authoritative_addresses(exchange) for exchange in dict.fromkeys(exchange for _, exchange in destinations)}
    return MxEvidence(domain, tuple(MxRecord(priority, exchange, addresses[exchange]) for priority, exchange in sorted(destinations)))
