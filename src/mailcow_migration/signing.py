import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import quote

from .configuration import Configuration
from .core.dns import cname_target, record_name, text_records
from .core.errors import ConfigurationError, MigrationError
from .core.mail_network import missing_spf_entries, verify_dkim
from .core.state import OperationJournal, Workspace, reconcile_observed_mutation, require_identity
from .core.storage import json_fingerprint, local_path, read_json, write_private_json
from .handoff_state import handoff_identity, require_handoff_ready
from .plans.saved import SavedPlan, load_saved_plan
from .targets.mailcow import MailcowTarget
from .targets.reconciliation import api_number
from .transfers import require_mailcow_host


def mailcow_dkim(target: MailcowTarget, domain: str) -> Mapping[str, str]:
    value = target.api.request("get/dkim/" + quote(domain, safe=""), object_response=True)
    if not isinstance(value, dict):
        raise MigrationError("Invalid Mailcow DKIM response; contents omitted")
    key, selector = value.get("pubkey") or "", value.get("dkim_selector") or ""
    if (not isinstance(key, str) or not isinstance(selector, str)
            or key and (not re.fullmatch(r"[a-zA-Z0-9+/]{40,}={0,2}", re.sub(r"\s", "", key))
                        or not re.fullmatch(r"[a-zA-Z0-9_.-]+", selector))):
        raise MigrationError("Invalid Mailcow DKIM public values")
    return {"pubkey": re.sub(r"\s", "", key), "dkim_selector": selector}


def domain_relay(target: MailcowTarget, domain: str) -> Mapping[str, Any]:
    found = tuple(row for row in target.api.inventory("domain") if (row.get("domain_name") or row.get("domain")) == domain)
    if len(found) != 1 or api_number(found[0].get("relayhost")) is None:
        raise MigrationError("Cannot read the domain's outgoing relay assignment")
    return found[0]


def assign_domain_relay(
    batch: SavedPlan, configuration: Configuration, target: MailcowTarget, identifier: str, *, apply: bool, require_complete: bool = False,
) -> Mapping[str, Any]:
    if not re.fullmatch(r"[1-9][0-9]*", identifier):
        raise ConfigurationError("Select a positive Mailcow relay ID")
    relays = tuple(row for row in target.api.inventory("relayhost")
                   if api_number(row.get("active")) == 1 and api_number(row.get("id")) == int(identifier))
    if len(relays) != 1 or not isinstance(relays[0].get("hostname"), str) or not relays[0]["hostname"]:
        raise MigrationError("Selected outgoing relay is missing, inactive or ambiguous")
    relay = {"id": identifier, "hostname": relays[0]["hostname"], "username": relays[0].get("username") or ""}
    journal = OperationJournal(Workspace(batch.directory), "outgoing-relay.json", {
        **handoff_identity(batch, configuration, "mailcow-outgoing-relay"), "relay": relay,
    })
    state = journal.read()
    if require_complete and (state.get("complete") is not True or "domain-relay" not in state["done"]):
        raise MigrationError("Complete outgoing relay assignment and retain its journal")
    payload = {"items": [batch.plan["domain"]], "attr": {"relayhost": identifier}}
    current = domain_relay(target, batch.plan["domain"])
    if "domain-relay" in state["done"] and api_number(current["relayhost"]) != int(identifier):
        raise MigrationError("The verified domain relay changed; reconcile the original assignment")

    def verify_overrides() -> None:
        mailboxes = tuple(row for row in target.api.inventory("mailbox") if row.get("domain") == batch.plan["domain"])
        if any(not isinstance(row.get("attributes"), dict) or api_number(row["attributes"].get("relayhost")) not in (0, int(identifier))
               for row in mailboxes):
            raise MigrationError("Mailbox relay overrides are unreadable or conflict; set them to inherit or the chosen relay")
        if not {row["username"] for row in batch.plan["mailboxes"]} <= {row.get("username") for row in mailboxes}:
            raise MigrationError("Planned mailbox inventory is incomplete while verifying relay inheritance")

    verify_overrides()
    reconcile_observed_mutation(journal, "domain-relay", "edit/domain", payload, api_number(current["relayhost"]) == int(identifier))
    if journal.read().get("pending"):
        raise MigrationError("An unknown outgoing-relay operation remains pending")
    if apply:
        if api_number(domain_relay(target, batch.plan["domain"])["relayhost"]) != int(identifier):
            journal.write_mutation("domain-relay", "edit/domain", payload, target.api.request, apply=True)
        if api_number(domain_relay(target, batch.plan["domain"])["relayhost"]) != int(identifier):
            raise MigrationError("Outgoing domain relay did not verify")
        verify_overrides()
        journal.save({**journal.read(), "done": ["domain-relay"], "complete": True, "verified_at": datetime.now(timezone.utc).isoformat()})
    return relay


def signing_choice(batch: SavedPlan, configuration: Configuration) -> Mapping[str, Any]:
    file = local_path(batch.directory / "signing-choice.json")
    if not file.exists():
        raise MigrationError("Run signing preview and --apply to record the intended DKIM signer and relay before handoff")
    choice = read_json(file, private=True)
    if (not isinstance(choice, dict) or type(choice.get("version")) is not int or choice.get("complete") is not True
            or choice.get("signer") not in ("mailcow", "relay")):
        raise MigrationError("Complete the signing selection and required key/relay operations before DNS verification")
    require_identity(choice, handoff_identity(batch, configuration, "mail-signing-choice"))
    return choice


def run_signing(
    configuration: Configuration, file: Path, *, signer: Optional[str] = None, dkim_name: Optional[str] = None,
    cname: Optional[str] = None, public_key: Optional[str] = None, key_type: Optional[str] = None, relay_id: Optional[str] = None,
    apply: bool = False,
) -> int:
    require_mailcow_host()
    if signer not in (None, "mailcow", "relay") or key_type not in (None, "rsa", "ed25519") or cname and public_key:
        raise ConfigurationError("Choose mailcow/relay signing and at most one provider CNAME or public key")
    with Workspace().lock():
        batch = load_saved_plan(file, configuration)
        target = require_handoff_ready(batch, configuration)
        identity = handoff_identity(batch, configuration, "mail-signing-choice")
        choice_file = local_path(batch.directory / "signing-choice.json")
        previous = read_json(choice_file, private=True) if choice_file.exists() else None
        if previous is not None:
            if (not isinstance(previous, dict) or type(previous.get("version")) is not int
                    or type(previous.get("complete")) is not bool or previous.get("signer") not in ("mailcow", "relay")):
                raise MigrationError("Invalid signing choice; retain it")
            require_identity(previous, identity)
        selected = signer or (previous or {}).get("signer") or "mailcow"
        key_type = key_type or (previous or {}).get("key_type") or "rsa"
        if key_type not in ("rsa", "ed25519"):
            raise MigrationError("Invalid saved DKIM key type")
        if selected == "relay":
            name = record_name((dkim_name or (previous or {}).get("name") or "").lower().rstrip("."))
            if not re.fullmatch(r"[a-z0-9_.-]+\._domainkey\." + re.escape(batch.plan["domain"]), name):
                raise ConfigurationError("The provider DKIM name must belong to this primary domain")
            delegated = (None if public_key else cname or (previous or {}).get("cname")
                         or (cname_target(name) if previous is None else None))
            key = public_key or (previous or {}).get("key") or ""
            if delegated:
                delegated = record_name(delegated.lower().rstrip("."))
                expected = {"name": name, "cname": delegated}
            else:
                key = re.sub(r"\s", "", key)
                if not re.fullmatch(r"[a-zA-Z0-9+/]{40,}={0,2}", key):
                    raise ConfigurationError("Supply the provider CNAME or public key; existing provider DNS is not guessed")
                expected = {"name": name, "key": key, "key_type": key_type}
            saved_relay = (previous or {}).get("relay", {})
            if not isinstance(saved_relay, dict):
                raise MigrationError("Invalid saved relay choice")
            chosen_id = relay_id or str(saved_relay.get("id") or "")
            if not chosen_id:
                for row in target.api.inventory("relayhost"):
                    if api_number(row.get("active")) == 1:
                        print("Available relay ID {}: {}.".format(row.get("id"), row.get("hostname")))
                raise ConfigurationError("Select the existing signing provider with --relay-id; mailbox inheritance will be checked")
            relay = assign_domain_relay(batch, configuration, target, chosen_id, apply=False)
            proposed = {**identity, "signer": selected, **expected, "relay": relay}
        else:
            if any((dkim_name, cname, public_key, relay_id)):
                raise ConfigurationError("Provider DKIM and relay options apply only to relay signing")
            proposed = {**identity, "signer": selected}
        if previous is not None:
            comparable = {key: value for key, value in previous.items() if key not in ("complete", "dkim", "configured_at")}
            if comparable != proposed:
                raise MigrationError("Signing choice differs from its saved configuration; retain the original choice and reconcile")
        if selected == "relay":
            if apply:
                if previous is None or not previous["complete"]:
                    write_private_json(choice_file, {**proposed, "complete": False}, replace=True)
                assign_domain_relay(batch, configuration, target, chosen_id, apply=True)
            print("Provider DKIM: {} {} {}".format(name, "CNAME" if delegated else "TXT",
                                                  delegated or "v=DKIM1; k={}; p={}".format(key_type, key)))
        else:
            dkim = mailcow_dkim(target, batch.plan["domain"])
            if previous and previous.get("dkim") and previous["dkim"] != dkim:
                raise MigrationError("Mailcow DKIM key changed or disappeared after selection; reconcile without rotating keys")
            if apply and (previous is None or not previous["complete"]):
                write_private_json(choice_file, {**proposed, **({"dkim": dkim} if dkim["pubkey"] else {}), "complete": False}, replace=True)
            journal = OperationJournal(Workspace(batch.directory), "dkim.json", handoff_identity(batch, configuration, "mailcow-dkim-key"))
            payload = {"domains": batch.plan["domain"], "dkim_selector": "dkim", "key_size": "2048"}
            state = journal.read()
            reconcile_observed_mutation(journal, "generate-key", "add/dkim", payload,
                                        bool(dkim["pubkey"]) and dkim["dkim_selector"] == "dkim")
            if journal.read().get("pending"):
                raise MigrationError("An unknown DKIM operation remains pending")
            if not dkim["pubkey"]:
                if "generate-key" in state["done"]:
                    raise MigrationError("Previously generated DKIM key disappeared; no key rotation attempted")
                if not apply:
                    print("Would generate the missing 2048-bit Mailcow DKIM key, selector dkim; existing keys are retained.")
                    return 0
                journal.write_mutation("generate-key", "add/dkim", payload, target.api.request, apply=True)
                dkim = mailcow_dkim(target, batch.plan["domain"])
            if not dkim["pubkey"] or not dkim["dkim_selector"]:
                raise MigrationError("Mailcow DKIM public key could not be read back")
            if "generate-key" in journal.read()["done"] and dkim["dkim_selector"] != "dkim":
                raise MigrationError("Generated DKIM selector differs from its request")
            proposed = {**proposed, "dkim": dkim}
            print("Publish {}._domainkey.{} TXT v=DKIM1; k=rsa; p={}".format(
                dkim["dkim_selector"], batch.plan["domain"], dkim["pubkey"],
            ))
        if apply and (previous is None or not previous["complete"]):
            write_private_json(choice_file, {**proposed, "complete": True, "configured_at": datetime.now(timezone.utc).isoformat()}, replace=True)
        print("Signing configuration {}. DNS publication and received-message authentication checks remain separate.".format(
            "verified and saved" if apply else "previewed; no key or relay writes",
        ))
    return 0


def verify_signing_dns(batch: SavedPlan, configuration: Configuration, addresses: tuple) -> Mapping[str, Any]:
    choice = signing_choice(batch, configuration)
    policies = tuple(text for text in text_records(batch.plan["domain"]) if re.match(r"^v=spf1(?:\s|$)", text, re.I))
    if len(policies) != 1:
        raise MigrationError("Publish exactly one SPF policy for the primary domain")
    missing = missing_spf_entries(policies[0], addresses)
    if missing:
        raise MigrationError("Add these explicit Mailcow IP entries to the existing SPF policy before its all term: " + " ".join(missing))
    target = MailcowTarget(configuration.mailcow)
    if choice["signer"] == "relay":
        relay = choice["relay"]
        verified = assign_domain_relay(batch, configuration, target, relay["id"], apply=False, require_complete=True)
        if verified != relay or api_number(domain_relay(target, batch.plan["domain"])["relayhost"]) != int(relay["id"]):
            raise MigrationError("Outgoing relay assignment differs from the saved signer")
        name = choice["name"]
        verify_dkim(name, cname=choice.get("cname", ""), key=choice.get("key", ""), kind=choice.get("key_type", "rsa"))
    else:
        current = mailcow_dkim(target, batch.plan["domain"])
        if current != choice["dkim"]:
            raise MigrationError("Mailcow DKIM changed after signer selection")
        name = record_name(current["dkim_selector"].lower() + "._domainkey." + batch.plan["domain"])
        verify_dkim(name, key=current["pubkey"])
    result = {**handoff_identity(batch, configuration, "mail-signing-verification"), "spf": policies, "ips": addresses,
              "name": name, "choice_sha256": json_fingerprint(choice, canonical=True), "complete": True,
              "checked_at": datetime.now(timezone.utc).isoformat()}
    write_private_json(batch.directory / "signing-verified.json", result, replace=True)
    return result
