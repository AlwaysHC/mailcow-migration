import re
import time
from datetime import datetime, timezone
from pathlib import Path
from shutil import which
from typing import Any, Mapping, Optional, Tuple

from .configuration import Configuration
from .core.commands import run_command
from .core.dns import authoritative_addresses, normalized_ip
from .core.domain import domain_name
from .core.errors import ConfigurationError, MigrationError
from .core.mail_network import tls_endpoints
from .core.state import Workspace, require_identity
from .core.storage import (
    json_fingerprint,
    local_path,
    read_json,
    read_text,
    write_atomic_text,
    write_private_json,
    write_private_text,
)
from .handoff_state import handoff_identity, require_handoff_ready
from .plans.saved import SavedPlan, load_saved_plan
from .sources.inventory import inspect_source
from .targets.mailcow import MailcowTarget
from .transfers import require_mailcow_host

ENVIRONMENT_KEYS = (
    "MAILCOW_HOSTNAME",
    "ACME_DNS_CHALLENGE",
    "ENABLE_SSL_SNI",
    "SKIP_LETS_ENCRYPT",
    "ONLY_MAILCOW_HOSTNAME",
    "LE_STAGING",
    "ADDITIONAL_SAN",
    "ADDITIONAL_SERVER_NAMES",
)


def certificate_environment(target: MailcowTarget) -> Mapping[str, Mapping[str, str]]:
    return target.compose_environment(
        ("acme-mailcow", "nginx-mailcow"), ENVIRONMENT_KEYS
    )


def certificate_covers(file: Path, hostname: str) -> bool:
    match = run_command(
        ("openssl", "x509", "-in", str(file), "-noout", "-checkhost", hostname),
        label="Check existing certificate hostname",
        private_output=True,
        environment={"LC_ALL": "C"},
    )
    accepted = "Hostname " + hostname + " does match certificate"
    rejected = "Hostname " + hostname + " does NOT match certificate"
    if match.stderr.strip() or match.stdout.strip() not in (accepted, rejected):
        raise MigrationError(
            "Cannot inspect the existing certificate; review it with its certificate manager"
        )
    if match.stdout.strip() == rejected:
        return False
    expiry = run_command(
        ("openssl", "x509", "-in", str(file), "-noout", "-checkend", "0"),
        allowed_codes=(0, 1),
        label="Check existing certificate expiry",
        private_output=True,
    )
    if expiry.stderr.strip():
        raise MigrationError("Cannot inspect existing certificate expiry")
    return expiry.returncode == 0


def certificate_request(
    batch: SavedPlan, identity: Mapping[str, Any]
) -> Optional[Mapping[str, Any]]:
    file = local_path(batch.directory / "mail-certificate-request.json")
    if not file.exists():
        return None
    previous = read_json(file, private=True)
    if (
        not isinstance(previous, dict)
        or type(previous.get("version")) is not int
        or type(previous.get("complete")) is not bool
        or previous.get("stage")
        not in (
            "write configuration",
            "refresh containers",
            "verify endpoints",
            "complete",
        )
        or any(
            not isinstance(previous.get(key), str)
            or not re.fullmatch(r"[a-f0-9]{64}", previous[key])
            for key in ("before_sha256", "configuration_sha256")
        )
        or not isinstance(previous.get("backups"), list)
        or not previous["backups"]
        or any(
            not isinstance(path, str)
            or local_path(Path(path)).parent != batch.directory
            for path in previous["backups"]
        )
    ):
        raise MigrationError("Invalid certificate request receipt; retain its backups")
    require_identity(previous, identity)
    backup = batch.directory / (
        "mailcow-cert-config-" + previous["before_sha256"] + ".bak"
    )
    if (
        str(backup) not in previous["backups"]
        or json_fingerprint(read_text(backup, private=True))
        != previous["before_sha256"]
    ):
        raise MigrationError("Certificate configuration backup changed or is missing")
    return previous


def wait_for_certificate(
    target: MailcowTarget,
    batch: SavedPlan,
    hostname: str,
    addresses: Tuple[str, ...],
    identity: Mapping[str, Any],
    seconds: int,
) -> None:
    request = certificate_request(batch, identity)
    if (
        request
        and not request["complete"]
        and json_fingerprint(
            read_text(Path(target.settings.directory) / "mailcow.conf"),
        )
        != request["configuration_sha256"]
    ):
        raise MigrationError(
            "Finish or reconcile the recorded certificate configuration update before verifying completion"
        )
    deadline, stable = time.monotonic() + seconds, 0
    write_private_json(
        batch.directory / "mail-certificate.json",
        {**identity, "ips": addresses, "complete": False},
        replace=True,
    )
    while True:
        endpoints = tls_endpoints(hostname, addresses)
        stable = stable + 1 if all(row["error"] is None for row in endpoints) else 0
        write_private_json(
            batch.directory / "mail-certificate-status.json",
            {
                **identity,
                "ips": addresses,
                "stable_checks": stable,
                "endpoints": endpoints,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            },
            replace=True,
        )
        print(
            "TLS endpoints passing: {}/{}; consecutive successful checks: {}/2.".format(
                sum(row["error"] is None for row in endpoints),
                len(endpoints),
                stable,
            ),
            flush=True,
        )
        for row in endpoints:
            if row["error"] is not None:
                print(
                    "{} {}:{}: {}".format(
                        row["service"], row["ip"], row["port"], row["error"]
                    ),
                    flush=True,
                )
        if stable == 2:
            target.compose(("exec", "-T", "nginx-mailcow", "nginx", "-t"))
            if certificate_request(batch, identity) != request:
                raise MigrationError("Certificate request changed during verification")
            if request is not None:
                write_private_json(
                    batch.directory / "mail-certificate-request.json",
                    {**request, "complete": True, "stage": "complete"},
                    replace=True,
                )
            write_private_json(
                batch.directory / "mail-certificate.json",
                {
                    **identity,
                    "ips": addresses,
                    "complete": True,
                    "endpoints": endpoints,
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                },
                replace=True,
            )
            return
        if time.monotonic() >= deadline:
            raise MigrationError(
                "Certificate checks remain incomplete. Inspect ACME/nginx logs locally, correct DNS/HTTP/CAA issues, "
                "then use certificate --action check or explicitly request a retry; retain the backup and status receipt"
            )
        time.sleep(10)


def run_certificate(
    configuration: Configuration,
    file: Path,
    *,
    hostname: Optional[str] = None,
    action: str = "preview",
    apply: bool = False,
    wait_seconds: int = 300,
) -> int:
    require_mailcow_host()
    if action not in ("preview", "apply", "check", "retry") or apply != (
        action in ("apply", "retry")
    ):
        raise ConfigurationError(
            "Use --apply with certificate apply/retry; preview/check do not change shared services"
        )
    if not 10 <= wait_seconds <= 3600:
        raise ConfigurationError("Certificate wait must be between 10 and 3600 seconds")
    with Workspace().lock():
        batch = load_saved_plan(file, configuration)
        hostname = domain_name(hostname or "mail." + batch.plan["domain"])
        target = require_handoff_ready(batch, configuration)
        identity = {
            **handoff_identity(batch, configuration, "mail-certificate"),
            "hostname": hostname,
        }
        request_file = local_path(batch.directory / "mail-certificate-request.json")
        previous = certificate_request(batch, identity)
        environment = certificate_environment(target)
        effective = environment["acme-mailcow"]
        main_hostname = domain_name(effective["MAILCOW_HOSTNAME"].lower().rstrip("."))
        allowed = authoritative_addresses(main_hostname)
        source = inspect_source(batch.source, batch.plan["domain"])
        if any(address in source.ips for address in allowed):
            raise MigrationError(
                "The Mailcow hostname still resolves to the source; correct target-host DNS before certificate setup"
            )
        addresses = authoritative_addresses(hostname)
        if not addresses or any(address not in allowed for address in addresses):
            raise MigrationError(
                "Mail hostname A/AAAA must point only to the Mailcow hostname's addresses before certificate setup"
            )
        initial = tls_endpoints(hostname, addresses)
        if all(row["error"] is None for row in initial) or action == "check":
            if action == "preview":
                print(
                    "All TLS endpoints currently pass. Use certificate --action check to record two stable checks."
                )
                return 0
            wait_for_certificate(
                target, batch, hostname, addresses, identity, wait_seconds
            )
            return 0
        if which("openssl") is None:
            raise MigrationError(
                "Automatic certificate setup requires the OpenSSL command-line tools on the Mailcow host"
            )
        directory = Path(configuration.mailcow.directory)
        separate = next(
            (
                directory / "data/assets/ssl" / name
                for name in (hostname, batch.plan["domain"])
                if all(
                    (directory / "data/assets/ssl" / name / item).is_file()
                    for item in ("cert.pem", "key.pem", "domains")
                )
                and hostname
                in (directory / "data/assets/ssl" / name / "domains")
                .read_text()
                .lower()
                .split()
                and main_hostname
                not in (directory / "data/assets/ssl" / name / "domains")
                .read_text()
                .lower()
                .split()
                and certificate_covers(
                    directory / "data/assets/ssl" / name / "cert.pem", hostname
                )
            ),
            None,
        )
        https_only = separate is not None and all(
            row["error"] is None or row["port"] == 443 for row in initial
        )
        separate_https = separate is not None or effective[
            "ENABLE_SSL_SNI"
        ].lower() in ("y", "yes")
        dns_challenge = effective["ACME_DNS_CHALLENGE"].lower() in ("y", "yes")
        config_file = local_path(directory / "mailcow.conf")
        before = read_text(config_file)
        if previous is not None:
            if not previous["complete"] and json_fingerprint(before) not in (
                previous["before_sha256"],
                previous["configuration_sha256"],
            ):
                raise MigrationError(
                    "Certificate configuration changed during an unfinished request; retain and reconcile its backups"
                )
            if (
                previous["configuration_sha256"] == json_fingerprint(before)
                and previous["stage"]
                in ("refresh containers", "verify endpoints", "complete")
                and action != "retry"
            ):
                if action == "preview":
                    print(
                        "A certificate request is already recorded. Use --action check, or --action retry --apply after correcting the cause."
                    )
                    return 0
                wait_for_certificate(
                    target, batch, hostname, addresses, identity, wait_seconds
                )
                return 0
        if not https_only:
            if any(
                effective[key].lower() in ("y", "yes")
                for key in ("SKIP_LETS_ENCRYPT", "ONLY_MAILCOW_HOSTNAME", "LE_STAGING")
            ):
                raise MigrationError(
                    "Review ACME skip/hostname-only/staging settings before requesting a trusted additional-host certificate"
                )
            cert = directory / "data/assets/ssl/cert.pem"
            if cert.exists():
                issuer = run_command(
                    ("openssl", "x509", "-in", str(cert), "-noout", "-issuer"),
                    label="Inspect certificate issuer",
                    private_output=True,
                ).stdout
                if not re.search(r"Let's Encrypt|mailcow|Fake LE Intermediate", issuer):
                    raise MigrationError(
                        "The existing certificate uses a custom issuer; add the hostname through that certificate manager"
                    )
            if not dns_challenge:
                inside = []
                for kind in ("A", "AAAA"):
                    output = target.compose(
                        (
                            "exec",
                            "-T",
                            "acme-mailcow",
                            "dig",
                            "+time=3",
                            "+tries=1",
                            "+short",
                            hostname,
                            kind,
                        )
                    ).stdout
                    for value in output.split():
                        if re.fullmatch(r"[0-9a-fA-F:.]+", value):
                            inside.append(normalized_ip(value))
                if not inside or any(address not in allowed for address in inside):
                    raise MigrationError(
                        "DNS inside acme-mailcow still differs; correct DNS/cache state before retrying certificate setup"
                    )
        updates, after = [], before
        for key, service in (
            ("ADDITIONAL_SAN", "acme-mailcow"),
            ("ADDITIONAL_SERVER_NAMES", "nginx-mailcow"),
        ):
            pattern = re.compile("^" + key + r"=(.*)$", re.MULTILINE)
            matches = tuple(pattern.finditer(before))
            if len(matches) > 1:
                raise MigrationError(
                    "Duplicate certificate configuration field: " + key
                )
            current = matches[0][1].strip() if matches else ""
            current = re.sub(r"""^(['"])(.*)\1$""", r"\2", current)
            if (
                not re.fullmatch(r"[a-zA-Z0-9*.,_-]*", current)
                or environment[service][key] != current
            ):
                raise MigrationError(
                    "Unsupported or overridden certificate configuration: " + key
                )
            names = tuple(name for name in current.split(",") if name)
            included = hostname == main_hostname or any(
                name.lower().rstrip(".") == hostname
                or key == "ADDITIONAL_SAN"
                and (
                    name.lower() == "mail.*"
                    or dns_challenge
                    and name.lower() == "*." + batch.plan["domain"]
                )
                for name in names
            )
            value = (
                ",".join(name for name in names if name.lower().rstrip(".") != hostname)
                if key == "ADDITIONAL_SERVER_NAMES" and separate_https
                else current
                if https_only or included
                else ",".join((*names, hostname))
            )
            updates.append((key, service, value))
            if value != current:
                after = (
                    pattern.sub(
                        lambda _, replacement=f"{key}={value}": replacement, after
                    )
                    if matches
                    else after.rstrip() + "\n" + key + "=" + value + "\n"
                )
            print("{}: {} -> {}".format(key, current or "(empty)", value or "(empty)"))
        print(
            "Shared service scope: "
            + (
                "nginx only; reuse the existing certificate."
                if https_only
                else "nginx and ACME; mail services may reload."
            )
        )
        if not apply:
            print(
                "Certificate preview completed; no configuration or services changed."
            )
            return 0
        if read_text(config_file) != before:
            raise MigrationError("Certificate configuration changed after preview")
        backup = batch.directory / (
            "mailcow-cert-config-" + json_fingerprint(before) + ".bak"
        )
        if backup.exists():
            if read_text(backup, private=True) != before:
                raise MigrationError(
                    "Certificate configuration backup differs; retain it"
                )
        else:
            write_private_text(backup, before)
        started = datetime.now(timezone.utc).isoformat()
        request = {
            **identity,
            "before_sha256": json_fingerprint(before),
            "configuration_sha256": json_fingerprint(after),
            "backups": list(
                dict.fromkeys([*(previous["backups"] if previous else []), str(backup)])
            ),
            "started_at": started,
            "complete": False,
        }
        write_private_json(
            request_file, {**request, "stage": "write configuration"}, replace=True
        )
        if before != after:
            write_atomic_text(config_file, after, replace=True)
        actual = certificate_environment(target)
        if any(actual[service][key] != value for key, service, value in updates):
            raise MigrationError(
                "Compose did not use the requested certificate settings; retain the configuration backup"
            )
        service = "nginx-mailcow" if https_only else "acme-mailcow"
        write_private_json(
            request_file, {**request, "stage": "refresh containers"}, replace=True
        )
        previous_container = target.compose(("ps", "-q", service)).stdout.strip()
        target.compose(
            (
                "up",
                "-d",
                "--no-deps",
                "nginx-mailcow",
                *(() if https_only else ("acme-mailcow",)),
            )
        )
        if target.compose(("ps", "-q", service)).stdout.strip() == previous_container:
            target.compose(("restart", service))
        write_private_json(
            request_file, {**request, "stage": "verify endpoints"}, replace=True
        )
        wait_for_certificate(target, batch, hostname, addresses, identity, wait_seconds)
    return 0
