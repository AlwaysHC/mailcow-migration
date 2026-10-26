import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from .certificates import certificate_environment, wait_for_certificate
from .configuration import Configuration
from .core.dns import authoritative_addresses, nameservers, query, source_mx
from .core.domain import domain_name
from .core.errors import ConfigurationError, MigrationError
from .core.state import Workspace, require_identity
from .core.storage import decode_json, json_fingerprint, local_path, read_json, write_private_json
from .handoff_state import handoff_identity, require_handoff_ready
from .plans.saved import SavedPlan, load_saved_plan
from .signing import signing_choice, verify_signing_dns
from .source_forwarding import verify_source_forwarding
from .source_selection import record_target_addresses
from .sources import SourceKind
from .sources.inventory import inspect_source
from .sources.plesk_isolation import PleskIsolation
from .sources.submission import SourceSubmission
from .targets.mailcow import MailcowTarget
from .transfers import require_mailcow_host


def mailbox_readability(batch: SavedPlan, configuration: Configuration, target: MailcowTarget) -> Mapping[str, Any]:
    file = local_path(batch.directory / "handoff-mailboxes.json")
    report = {**handoff_identity(batch, configuration, "handoff-mailbox-readability"), "complete": False, "users": []}
    write_private_json(file, report, replace=True)
    for index, row in enumerate(batch.plan["mailboxes"], 1):
        print("Mailbox readability {}/{}: {}.".format(index, len(batch.plan["mailboxes"]), row["username"]), flush=True)
        result = target.compose(("exec", "-T", "dovecot-mailcow", "doveadm", "-f", "flow", "mailbox", "status",
                                 "-u", row["username"], "messages unseen", "*"),
                                diagnostic_file=batch.directory / ("handoff-mailbox-{}.txt".format(index)))
        folders = []
        for line in result.stdout.splitlines():
            match = re.fullmatch(r"(.+?)\s+(messages=[0-9]+ unseen=[0-9]+|unseen=[0-9]+ messages=[0-9]+)", line)
            if match is None:
                raise MigrationError("Unexpected mailbox status output; private diagnostics retained for " + row["username"])
            values = {key: int(value) for field in match[2].split() for key, value in (field.split("="),)}
            if values["unseen"] > values["messages"] or any(value > 9007199254740991 for value in values.values()):
                raise MigrationError("Invalid mailbox message counters")
            folders.append({"folder": match[1], **values})
        if not folders:
            raise MigrationError("No readable folders returned for " + row["username"])
        messages, unseen = sum(row["messages"] for row in folders), sum(row["unseen"] for row in folders)
        if max(messages, unseen) > 9007199254740991:
            raise MigrationError("Mailbox totals exceed the supported range")
        report = {**report, "users": [*report["users"], {"username": row["username"], "active": row["active"],
                                                       "folders": folders, "messages": messages, "unseen": unseen}]}
        write_private_json(file, report, replace=True)
    report = {**report, "complete": True, "checked_at": datetime.now(timezone.utc).isoformat()}
    write_private_json(file, report, replace=True)
    return report


def inspect_handoff_queues(batch: SavedPlan, configuration: Configuration, target: MailcowTarget) -> Mapping[str, Any]:
    output = target.compose(("exec", "-T", "postfix-mailcow", "postqueue", "-j")).stdout
    try:
        rows = tuple(decode_json(line) for line in output.splitlines() if line.strip())
    except ValueError as error:
        raise MigrationError("Invalid target JSON queue output; contents omitted") from error
    if any(not isinstance(row, dict) or not isinstance(row.get("queue_id"), str)
           or not re.fullmatch(r"[A-Za-z0-9]+", row["queue_id"]) or not isinstance(row.get("queue_name"), str)
           or not isinstance(row.get("recipients"), list)
           or any(not isinstance(item, dict) or not isinstance(item.get("address"), str) for item in row["recipients"]) for row in rows):
        raise MigrationError("Unsupported target queue record")

    def belongs(address: Any) -> bool:
        return isinstance(address, str) and address.rsplit("@", 1)[-1].lower() in batch.plan["domains"]

    target_rows = tuple(row for row in rows if belongs(row.get("sender")) or any(belongs(item["address"]) for item in row["recipients"]))
    source_text = batch.source.ssh.run(("postqueue", "-p"),
                                     diagnostic_file=batch.directory / "handoff-source-queue.txt").stdout
    if not re.search(r"Mail queue is empty|^-[A-Za-z -]*Queue ID|^\s*-Queue ID-", source_text, re.M):
        raise MigrationError("Cannot interpret source queue listing")
    pattern = r"@(?:" + "|".join(re.escape(domain) for domain in batch.plan["domains"]) + r")(?=[\s>,)]|$)"
    source_rows = tuple(block for block in re.split(r"(?=^[A-Za-z0-9]+[*!]?\s+[0-9]+\s)", source_text, flags=re.M)
                        if re.search(pattern, block, re.I))
    report = {**handoff_identity(batch, configuration, "handoff-queue-inspection"),
              "checked_at": datetime.now(timezone.utc).isoformat(), "source": source_rows, "target": target_rows}
    write_private_json(batch.directory / "handoff-queues.json", report, replace=True)
    print("Batch queues: {} source entries; {} target entries. Details are retained privately.".format(len(source_rows), len(target_rows)))
    if source_rows or any(row["queue_name"] not in ("active", "incoming", "maildrop") for row in target_rows):
        raise MigrationError("Source batch mail remains queued or target delivery is deferred/held; inspect handoff-queues.json before completing")
    return report


def run_handoff(
    configuration: Configuration, file: Path, *, hostname: Optional[str] = None, action: str = "preview", apply: bool = False,
    delivery_confirmed: bool = False, clients_confirmed: bool = False, message_auth_confirmed: bool = False,
    permissions_confirmed: bool = False,
) -> int:
    require_mailcow_host()
    if action not in ("preview", "check", "complete") or apply != (action == "complete"):
        raise ConfigurationError("Use --apply only with handoff complete; preview/check never mark a batch complete")
    confirmations = {"delivery": delivery_confirmed, "clients": clients_confirmed,
                     "message_authentication": message_auth_confirmed, "sender_permissions": permissions_confirmed}
    if action != "complete" and any(confirmations.values()):
        raise ConfigurationError("Operator confirmations apply only to handoff complete")
    with Workspace().lock():
        batch = load_saved_plan(file, configuration)
        target = require_handoff_ready(batch, configuration)
        hostname = domain_name(hostname or "mail." + batch.plan["domain"])
        identity = {**handoff_identity(batch, configuration, "mail-handoff"), "hostname": hostname}
        completed_file = local_path(batch.directory / "completed.json")
        if completed_file.exists():
            previous = read_json(completed_file, private=True)
            if (not isinstance(previous, dict) or type(previous.get("version")) is not int or previous.get("complete") is not True
                    or not isinstance(previous.get("checks_sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", previous["checks_sha256"])
                    or previous.get("confirmations") != dict.fromkeys(confirmations, True)):
                raise MigrationError("Invalid completion record")
            require_identity(previous, {**identity, "kind": "mail-migration-complete"})
            archived = read_json(batch.directory / ("handoff-checks-" + previous["checks_sha256"] + ".json"), private=True)
            if json_fingerprint(archived, canonical=True) != previous["checks_sha256"]:
                raise MigrationError("Recorded completion evidence changed; retain and reconcile the original report")
        before_file = local_path(batch.directory / "dns-before.json")
        if not before_file.exists():
            records = [{"domain": domain, "server": server, "records": [asdict(row) for row in query(domain, "MX", server)]}
                       for domain in batch.plan["domains"] for server in nameservers(domain)]
            write_private_json(before_file, {**identity, "records": records, "captured_at": datetime.now(timezone.utc).isoformat()})
        else:
            require_identity(read_json(before_file, private=True), identity)
        environment = certificate_environment(target)
        main_hostname = domain_name(environment["acme-mailcow"]["MAILCOW_HOSTNAME"].lower().rstrip("."))
        allowed, addresses = authoritative_addresses(main_hostname), authoritative_addresses(hostname)
        source = inspect_source(batch.source, batch.plan["domain"])
        if any(address in source.ips for address in allowed) or any(address not in allowed for address in addresses):
            raise MigrationError("Client hostname must point to Mailcow addresses distinct from the source")
        record_target_addresses(batch, configuration)
        choice = signing_choice(batch, configuration)
        print("DNS handoff: point all batch MX destinations directly at Mailcow A/AAAA addresses: " + ", ".join(addresses))
        print("Keep one SPF policy with explicit Mailcow IP entries, preserve its other mechanisms, and publish the chosen DKIM record.")
        print("Keep source forwarding operational through the previous DNS TTL. Check inbound/outbound mail, aliases and catch-all behavior.")
        print("Users must verify passwords, folders, contacts, filters and attachments. Keep planned disabled users disabled.")
        print("Check authorized and rejected send-as/BCC cases and received SPF/DKIM/DMARC results, including relay Return-Path alignment.")
        if action == "preview":
            print("Handoff preview finished. DNS records and mail services were not changed.")
            return 0
        report_file = batch.directory / "handoff-checks.json"
        write_private_json(report_file, {**identity, "complete": False}, replace=True)
        wait_for_certificate(target, batch, hostname, addresses,
                             {**handoff_identity(batch, configuration, "mail-certificate"), "hostname": hostname}, 10)
        endpoints = read_json(batch.directory / "mail-certificate.json", private=True)["endpoints"]
        mx = tuple(source_mx(domain) for domain in batch.plan["domains"])
        if any(address not in addresses for evidence in mx for address in evidence.ips):
            raise MigrationError("Every authoritative MX destination must point only to the verified Mailcow addresses")
        write_private_json(batch.directory / "dns-verified.json", {
            **identity, "ips": addresses, "mx": [asdict(row) for row in mx], "checked_at": datetime.now(timezone.utc).isoformat(),
        }, replace=True)
        signing = verify_signing_dns(batch, configuration, addresses)
        forwarding = verify_source_forwarding(batch, configuration)
        if batch.source.kind is SourceKind.PLESK:
            PleskIsolation(batch, configuration).check(webmail=True)
        else:
            SourceSubmission(batch, configuration).check()
        mailboxes = mailbox_readability(batch, configuration, target)
        queues = inspect_handoff_queues(batch, configuration, target)
        require_handoff_ready(batch, configuration)
        report = {**identity, "complete": True, "ips": addresses, "endpoints": endpoints,
                  "signing_choice_sha256": json_fingerprint(choice, canonical=True),
                  "signing_sha256": json_fingerprint(signing, canonical=True), "forwarding_sha256": json_fingerprint(forwarding, canonical=True),
                  "mailboxes_sha256": json_fingerprint(mailboxes, canonical=True), "queues_sha256": json_fingerprint(queues, canonical=True),
                  "evidence": {"signing": signing, "forwarding": forwarding, "mailboxes": mailboxes, "queues": queues},
                  "checked_at": datetime.now(timezone.utc).isoformat()}
        write_private_json(report_file, report, replace=True)
        fingerprint = json_fingerprint(report, canonical=True)
        archive = batch.directory / ("handoff-checks-" + fingerprint + ".json")
        if not archive.exists():
            write_private_json(archive, report)
        if action == "complete":
            if not all(confirmations.values()) and not completed_file.exists():
                raise ConfigurationError("Live checks passed. Confirm actual delivery, client, message-authentication and permission checks "
                                         "with the four confirmation flags before marking this batch complete")
            completion = {**identity, "kind": "mail-migration-complete", "complete": True, "confirmations": confirmations,
                          "checks_sha256": fingerprint, "completed_at": datetime.now(timezone.utc).isoformat()}
            if not completed_file.exists():
                write_private_json(completed_file, completion)
            print("Migration completion is recorded. Retain journals, backups and source forwarding until cached DNS routes expire.")
        else:
            print("Automated handoff checks passed; prior completion is retained." if completed_file.exists() else
                  "Automated handoff checks passed. Operator delivery/client/authentication/permission confirmation is still required.")
    return 0
