import ipaddress
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .configuration import Configuration
from .core.errors import ConfigurationError, MigrationError
from .core.state import Workspace, require_identity
from .core.storage import local_path, read_json, write_private_json
from .handoff_state import handoff_identity, require_handoff_ready
from .plans.saved import SavedPlan, load_saved_plan
from .sources import SourceKind
from .sources.delivery import SourceDelivery
from .sources.inventory import inspect_source
from .sources.php import source_php
from .sources.plesk_isolation import PleskIsolation
from .sources.submission import SourceSubmission
from .transfers import require_mailcow_host


def forwarding_address(value: str) -> str:
    try:
        destination = ipaddress.ip_address(value)
    except ValueError as error:
        raise ConfigurationError(
            "Use a fixed Mailcow IPv4/IPv6 address reachable from the source on TCP 25"
        ) from error
    destination = getattr(destination, "ipv4_mapped", None) or destination
    if (
        "%" in value
        or destination.is_loopback
        or destination.is_unspecified
        or destination.is_multicast
        or destination.is_link_local
    ):
        raise ConfigurationError(
            "Use a reachable unicast Mailcow address distinct from the source"
        )
    return str(destination)


def forwarding_policy(plan: Mapping[str, Any], address: str) -> Mapping[str, Any]:
    policies = []
    for domain in plan["domains"]:
        enabled = bool(
            plan["domainActive"]
            and (
                domain == plan["domain"]
                or any(
                    row["alias_domain"] == domain and row["active"]
                    for row in plan["aliasDomains"]
                )
            )
        )
        recipients, rejected = [], []
        for row in plan["aliases"]:
            if row["address"].startswith("@"):
                continue
            accepted = (
                enabled
                and bool(row["active"])
                and (
                    not row["mailbox"]
                    or any(
                        user["username"] == row["address"] and user["active"]
                        for user in plan["mailboxes"]
                    )
                )
            )
            (recipients if accepted else rejected).append(
                row["address"].split("@")[0] + "@" + domain
            )
        policies.append(
            {
                "domain": domain,
                "recipients": sorted(set(recipients)),
                "rejected": sorted(set(rejected)),
                "catchAll": enabled
                and any(
                    row["address"] == "@" + plan["domain"] and row["active"]
                    for row in plan["aliases"]
                ),
            }
        )
    return {"ip": address, "policies": policies}


def forwarding_request(
    batch: SavedPlan,
    configuration: Configuration,
    address: str,
    *,
    apply: bool = False,
    fingerprint: str = "",
) -> Mapping[str, Any]:
    address = forwarding_address(address)
    if apply and not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
        raise MigrationError(
            "Forwarding writes require the current preview fingerprint"
        )
    if batch.source.kind is SourceKind.PLESK:
        return PleskIsolation(batch, configuration).request(
            "forward" if apply else "forward-preview",
            forwarding=forwarding_policy(batch.plan, address),
            **({"fingerprint": fingerprint} if apply else {}),
        )
    base = SourceDelivery(batch, configuration.mailcow.api).routing_request
    host = "IPv6:" + address if ipaddress.ip_address(address).version == 6 else address
    return source_php(
        batch.source,
        batch.directory,
        "postfixadmin-routing.php",
        {
            **base,
            "action": "apply" if apply else "inspect",
            "isolation": False,
            "ip": address,
            "entry": {**base["entry"], "route": f"smtp:[{host}]:25"},
            **({"fingerprint": fingerprint} if apply else {}),
        },
        shared=("postfixadmin-commands.php",),
        timeout=300,
    )


def verify_source_forwarding(
    batch: SavedPlan, configuration: Configuration
) -> Mapping[str, Any]:
    file = local_path(batch.directory / "source-forwarding.json")
    if not file.exists():
        raise MigrationError(
            "Run source-forward preview and --apply before completing handoff"
        )
    state = read_json(file, private=True)
    expected = handoff_identity(batch, configuration, "source-forwarding")
    if (
        not isinstance(state, dict)
        or type(state.get("version")) is not int
        or state.get("complete") is not True
        or not isinstance(state.get("ip"), str)
    ):
        raise MigrationError(
            "Finish the recorded source-forwarding operation before handoff completion"
        )
    require_identity(state, expected)
    preview = forwarding_request(batch, configuration, state["ip"])
    if preview.get("complete") is not True:
        raise MigrationError("Source forwarding no longer matches its receipt")
    return state


def run_source_forwarding(
    configuration: Configuration, file: Path, address: str, *, apply: bool = False
) -> int:
    require_mailcow_host()
    address = forwarding_address(address)
    with Workspace().lock():
        batch = load_saved_plan(file, configuration)
        require_handoff_ready(batch, configuration)
        source = inspect_source(batch.source, batch.plan["domain"])
        if address in source.ips:
            raise MigrationError(
                "The forwarding destination belongs to the source server"
            )
        if batch.source.kind is SourceKind.PLESK:
            PleskIsolation(batch, configuration).check(webmail=True)
        else:
            SourceSubmission(batch, configuration).check()
        journal = local_path(batch.directory / "source-forwarding.json")
        identity = {
            **handoff_identity(batch, configuration, "source-forwarding"),
            "ip": address,
        }
        if journal.exists():
            previous = read_json(journal, private=True)
            if (
                not isinstance(previous, dict)
                or type(previous.get("complete")) is not bool
            ):
                raise MigrationError("Invalid source-forwarding journal; retain it")
            require_identity(previous, identity)
        preview = forwarding_request(batch, configuration, address)
        if (
            not isinstance(preview.get("fingerprint"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", preview["fingerprint"])
            or type(preview.get("complete")) is not bool
            or not isinstance(preview.get("queue"), dict)
        ):
            raise MigrationError(
                "Invalid source forwarding preview; private diagnostics retained"
            )
        print(
            "Forward {} to {}:25, retaining source login/submission blocks and original recipient policy.".format(
                ", ".join(batch.plan["domains"]),
                address,
            )
        )
        print(
            "Apply starts real delivery to Mailcow. Source queue scheduling remains scoped; no global flush, requeue or deletion."
        )
        if not apply:
            print(
                "Forwarding preview completed; no source route or forwarding receipt was changed."
            )
            return 0
        write_private_json(
            journal,
            {**identity, "complete": False, "stage": "apply forwarding"},
            replace=True,
        )
        if not preview["complete"]:
            forwarding_request(
                batch,
                configuration,
                address,
                apply=True,
                fingerprint=preview["fingerprint"],
            )
        current = forwarding_request(batch, configuration, address)
        if current.get("complete") is not True:
            raise MigrationError(
                "Forwarding readback failed; keep source and local recovery records"
            )
        write_private_json(
            journal,
            {
                **identity,
                "complete": True,
                "stage": "complete",
                "result": current,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            },
            replace=True,
        )
        print(
            "Source forwarding verified. Logins remain blocked; receipt: "
            + str(journal)
        )
    return 0
