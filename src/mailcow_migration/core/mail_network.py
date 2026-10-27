import ipaddress
import re
import socket
import ssl
from typing import Any, Callable, Mapping, Sequence, Tuple

from .dns import cname_target, record_name, text_records
from .errors import MigrationError

TLS_SERVICES = (
    (443, "HTTPS"),
    (993, "IMAPS"),
    (995, "POP3S"),
    (465, "SMTPS"),
    (587, "SMTP STARTTLS"),
)


def smtp_reply(stream: Any, expected: int) -> Tuple[bytes, ...]:
    total = 0
    lines = []
    for _ in range(100):
        line = stream.readline(4097)
        total += len(line)
        if (
            len(line) > 4096
            or total > 65536
            or not re.fullmatch(rb"[0-9]{3}[ -][^\r\n]*\r?\n", line)
        ):
            raise MigrationError("Incomplete or excessive SMTP capability response")
        if int(line[:3]) != expected:
            raise MigrationError(
                "Unexpected SMTP capability status: " + line[:3].decode("ascii")
            )
        lines.append(line)
        if line[3:4] == b" ":
            return tuple(lines)
    raise MigrationError("Excessive SMTP multiline response")


def check_tls(hostname: str, address: str, port: int) -> None:
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    with socket.create_connection((address, port), timeout=15) as connection:
        if port == 587:
            with connection.makefile("rb") as stream:
                smtp_reply(stream, 220)
                connection.sendall(b"EHLO migration-check.invalid\r\n")
                capabilities = smtp_reply(stream, 250)
                if not any(
                    line[4:].strip().upper() == b"STARTTLS" for line in capabilities
                ):
                    raise MigrationError("SMTP does not advertise STARTTLS")
                connection.sendall(b"STARTTLS\r\n")
                smtp_reply(stream, 220)
        with context.wrap_socket(connection, server_hostname=hostname):
            pass


def tls_endpoints(
    hostname: str,
    addresses: Sequence[str],
    tell: Callable[[str], None] = lambda message: print(message, flush=True),
) -> Tuple[Mapping[str, Any], ...]:
    results = []
    for address in addresses:
        for port, service in TLS_SERVICES:
            tell(f"Checking {service} TLS at {address}:{port} for {hostname}...")
            try:
                check_tls(hostname, address, port)
            except (OSError, MigrationError) as error:
                results.append(
                    {
                        "ip": address,
                        "port": port,
                        "service": service,
                        "error": str(error),
                    }
                )
            else:
                results.append(
                    {"ip": address, "port": port, "service": service, "error": None}
                )
    return tuple(results)


def missing_spf_entries(policy: str, addresses: Sequence[str]) -> Tuple[str, ...]:
    if not addresses:
        raise MigrationError("Mailcow public addresses are missing")
    networks = []
    for term in policy.split()[1:]:
        if re.fullmatch(r"[+~?-]?all", term, re.IGNORECASE):
            break
        match = re.fullmatch(r"\+?ip([46]):([^/]+)(?:/([0-9]+))?", term, re.IGNORECASE)
        if match is None:
            continue
        try:
            address = ipaddress.ip_address(match[2])
            network = ipaddress.ip_network(
                match[2] + "/" + (match[3] or ("32" if match[1] == "4" else "128")),
                strict=False,
            )
        except ValueError as error:
            raise MigrationError("Invalid explicit SPF IP entry") from error
        if "%" in match[2] or address.version != int(match[1]):
            raise MigrationError("Invalid SPF address family")
        networks.append(network)
    parsed = tuple(ipaddress.ip_address(address) for address in addresses)
    return tuple(
        f"ip{address.version}:{address}"
        for address in parsed
        if not any(
            address.version == network.version and address in network
            for network in networks
        )
    )


def published_dkim(name: str) -> Mapping[str, str]:
    keys = []
    for text in text_records(record_name(name)):
        if not re.search(r"(?:^|;)\s*p\s*=", text, re.IGNORECASE):
            continue
        tags = {}
        for part in filter(str.strip, text.split(";")):
            match = re.fullmatch(r"\s*([a-z]+)\s*=\s*(.*?)\s*", part, re.IGNORECASE)
            if match is None or match[1].lower() in tags:
                raise MigrationError("Malformed or duplicate DKIM tags at " + name)
            tags[match[1].lower()] = match[2]
        key, kind = re.sub(r"\s", "", tags.get("p", "")), tags.get("k", "rsa").lower()
        if (
            tags.get("v", "DKIM1") != "DKIM1"
            or kind not in ("rsa", "ed25519")
            or not re.fullmatch(r"[A-Za-z0-9+/]{40,}={0,2}", key)
        ):
            raise MigrationError(
                "Revoked, malformed or unsupported DKIM public key at " + name
            )
        keys.append({"key": key, "type": kind})
    if len(keys) != 1:
        raise MigrationError("Expected one DKIM public key at " + name)
    return keys[0]


def verify_dkim(
    name: str, *, cname: str = "", key: str = "", kind: str = "rsa"
) -> None:
    if cname and cname_target(name) != record_name(cname):
        raise MigrationError("Published DKIM CNAME differs at " + name)
    actual = published_dkim(name)
    if not cname and (
        actual["key"] != re.sub(r"\s", "", key) or actual["type"] != kind
    ):
        raise MigrationError("Published DKIM key/type differs at " + name)
