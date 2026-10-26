import hashlib
import ipaddress
import secrets
import smtplib


FORWARD_SETTINGS = ("always_bcc", "sender_bcc_maps", "recipient_bcc_maps", "canonical_maps",
                    "masquerade_domains", "content_filter", "smtpd_proxy_filter")


def forwarding_configuration(state):
    configuration = {name: postconf(name) for name in (
        *FORWARD_SETTINGS, "recipient_canonical_maps", "recipient_delimiter", "enable_original_recipient")}
    canonical = "recipient_canonical_maps"
    if canonical in state["postfix"]:
        original = state["postfix"][canonical]
        if configuration[canonical] == managed(canonical, original):
            configuration[canonical] = original
        elif configuration[canonical] != original:
            raise RuntimeError("Plesk recipient canonical maps changed outside migration")
    for name in FORWARD_SETTINGS:
        if configuration[name]:
            raise RuntimeError(f"Plesk forwarding requires a reviewed adapter for {name}; source rules were retained")
    if configuration["recipient_delimiter"] not in ("", "+"):
        raise RuntimeError("Plesk forwarding supports an empty or + recipient delimiter")
    if configuration["enable_original_recipient"] != "yes":
        raise RuntimeError("Plesk forwarding requires enable_original_recipient=yes; existing queues still need original-recipient history")
    if "no_address_mappings" in postconf("receive_override_options"):
        raise RuntimeError("Global no_address_mappings would bypass migration recipient maps")
    for line in command(["postconf", "-P"]).splitlines():
        key, separator, value = line.partition("=")
        if not separator:
            raise RuntimeError("Cannot parse Postfix service overrides")
        name = key.rsplit("/", 1)[-1]
        if name in (*configuration, "virtual_alias_maps"):
            raise RuntimeError(f"Postfix service override needs a forwarding adapter: {key}")
        if name == "receive_override_options" and "no_address_mappings" in value:
            raise RuntimeError(f"Service override bypasses migration recipient maps: {key}")
    return configuration


def forwarding_spec(request, batch, configuration):
    supplied = request["forwarding"]
    address = ipaddress.ip_address(supplied["ip"])
    if address.is_loopback or address.is_unspecified or address.is_multicast or address.is_link_local:
        raise RuntimeError("Use a reachable Mailcow address distinct from the Plesk server")
    local = json.loads(command(["ip", "-j", "address", "show"]))
    if any(address == ipaddress.ip_address(item["local"]) for link in local for item in link.get("addr_info", [])):
        raise RuntimeError("The forwarding destination is an address of Plesk itself")
    policies = supplied["policies"]
    if sorted(row["domain"] for row in policies) != sorted(batch["domains"]):
        raise RuntimeError("Forwarding policies do not cover exactly this batch's domains")
    for row in policies:
        if not isinstance(row["catchAll"], bool):
            raise RuntimeError("Invalid catch-all policy")
        addresses = row["recipients"] + row["rejected"]
        if len(addresses) != len(set(addresses)) or any(
                not re.fullmatch(r"[a-z0-9_+.-]+@" + re.escape(row["domain"]), value) for value in addresses):
            raise RuntimeError("Invalid or overlapping forwarding recipient policies")
    host = f"IPv6:{address}" if address.version == 6 else str(address)
    return {"ip": str(address), "route": f"smtp:[{host}]:25", "policies": policies,
            "delimiter": configuration["recipient_delimiter"]}


def recipient_pattern(address, delimiter):
    local, domain = address.split("@")
    extension = r"(\+[^@]*)?" if delimiter else ""
    return f"{re.escape(local)}{extension}@{re.escape(domain)}"


def forwarding_rules(forwarding):
    access, aliases = [], []
    for policy in forwarding["policies"]:
        for key, result in (("rejected", "550 5.1.1 Recipient disabled"), ("recipients", "DUNNO")):
            for address in policy[key]:
                pattern = recipient_pattern(address, forwarding["delimiter"])
                access.append(f"/^{pattern}$/ {result}\n")
                if key == "recipients":
                    aliases.append(f"/^({pattern})$/ $1\n")
        pattern = f"[^@]+@{re.escape(policy['domain'])}"
        access.append(f"/^{pattern}$/ " + ("DUNNO" if policy["catchAll"] else "550 5.1.1 Unknown recipient") + "\n")
        if policy["catchAll"]:
            aliases.append(f"/^({pattern})$/ $1\n")
    return "".join(access), "".join(aliases)


def forwarding_probes(forwarding):
    probes = {}
    for policy in forwarding["policies"]:
        for accepted, key in ((True, "recipients"), (False, "rejected")):
            for address in policy[key]:
                probes[address] = accepted
                if forwarding["delimiter"]:
                    local, domain = address.split("@")
                    probes[f"{local}+migration-{secrets.token_hex(6)}@{domain}"] = accepted
        probes[f"migration-{secrets.token_hex(12)}@{policy['domain']}"] = policy["catchAll"]
    return probes


def smtp_probe(host, probes):
    def expect(reply, codes, label):
        code, message = reply
        if code not in codes:
            detail = message.decode("utf8", errors="replace")
            raise RuntimeError(f"SMTP probe {host}:25 {label}: {code} {detail}")

    hostname = postconf("myhostname")
    with smtplib.SMTP(local_hostname=hostname, timeout=15) as smtp:
        greeting = smtp.connect(host, 25)
        expect(greeting, (220,), "greeting")
        if host != "127.0.0.1" and greeting[1].split()[0].decode("ascii", errors="replace").lower() == hostname.lower():
            raise RuntimeError("Mailcow's SMTP greeting identifies the Plesk host; resolve the potential routing loop")
        expect(smtp.ehlo(), (250,), "EHLO")
        for address, accepted in probes.items():
            expect(smtp.mail(""), (250,), "MAIL FROM")
            expect(smtp.rcpt(address), (250, 251) if accepted else (550, 551, 553), f"RCPT TO {address}")
            expect(smtp.rset(), (250,), "RSET")


def forwarding_accepts(address, forwarding):
    local, separator, domain = address.lower().rpartition("@")
    policy = next((row for row in forwarding["policies"] if row["domain"] == domain), None)
    if not separator or not policy:
        return False
    matches = lambda candidate: re.fullmatch(recipient_pattern(candidate, forwarding["delimiter"]), address, re.IGNORECASE)
    return not any(map(matches, policy["rejected"])) and (policy["catchAll"] or any(map(matches, policy["recipients"])))


def forwarding_queue(batch, forwarding):
    listing = command(["postqueue", "-p"])
    entries = re.findall(r"^([A-Za-z0-9]+)([*!]?)\s+\d+\s+", listing, re.MULTILINE)
    if not entries and "Mail queue is empty" not in listing:
        raise RuntimeError("Cannot interpret the Plesk Postfix queue listing")
    affected, shared = [], []
    in_batch = lambda address: address.lower().rpartition("@")[2] in batch["domains"]
    for identifier, status in entries:
        result = invoke(["postcat", "-q", "-e", identifier], allowed=(0, 1))
        if result.returncode:
            if not re.search(r"^" + re.escape(identifier) + r"[*!]?\s+\d+\s", command(["postqueue", "-p"]), re.MULTILINE):
                continue
            raise RuntimeError(f"Cannot inspect Plesk queue {identifier}; see diagnostics above")
        envelope = result.stdout
        if (not envelope.startswith("*** ENVELOPE RECORDS ") or "*** MESSAGE FILE END " not in envelope
                or not re.search(r"^sender: ", envelope, re.MULTILINE)):
            raise RuntimeError(f"Incomplete queue envelope for {identifier}")
        original, recipients, expanded, missing, filters, delivered = "", [], [], [], [], []
        for line in envelope.splitlines():
            if line.startswith("original_recipient: "):
                original = line[len("original_recipient: "):].lower()
            match = re.fullmatch(r"(recipient|done_recipient): (.+)", line)
            if match:
                address = match[2].lower()
                if in_batch(original) and original != address:
                    expanded.append(f"{original} -> {address}")
                if match[1] == "recipient":
                    recipients.append(address)
                    if in_batch(address) and not original:
                        missing.append(address)
                elif in_batch(address) or in_batch(original):
                    delivered.append(address)
                original = ""
            override = re.match(r"(?:named_attribute: )?(?:content_filter|redirect_target)[:= ]+(.*)", line)
            if override and override[1].strip():
                filters.append(line)
        if not any(map(in_batch, recipients)):
            continue
        problem = ("old alias expansion: " + ", ".join(expanded) if expanded else
                   "missing original-recipient history" if missing else
                   "batch recipients were already partly delivered on Plesk" if delivered and not batch.get("forwarding", {}).get("ready") else
                   "administratively held message" if status == "!" else
                   "per-message filter/redirect override" if filters else
                   "recipient is disabled or unknown on Mailcow" if any(
                       in_batch(address) and not forwarding_accepts(address, forwarding) for address in recipients) else None)
        if problem:
            raise RuntimeError(f"Plesk queue {identifier}: {problem}. Reconcile this message before forwarding; it was not changed")
        affected.append(identifier)
        if status == "*" or not all(map(in_batch, recipients)):
            shared.append(identifier)
    return {"affected": affected, "shared": shared}


def verify_forwarding_maps(batch, forwarding):
    ready = batch["forwarding"].get("ready", False)
    for address, accepted in forwarding_probes(forwarding).items():
        expected = "DUNNO" if accepted else None
        access = command(["postmap", "-q", address, f"regexp:{MAPS['recipients']}"]).strip()
        matches = access == DEFER if not ready else access == expected if accepted else access.startswith("550 ")
        if not matches:
            raise RuntimeError(f"Plesk recipient map does not preserve delivery policy for {address}")
        if accepted and command(["postmap", "-q", address, f"regexp:{ALIAS_MAP}"]).strip().lower() != address.lower():
            raise RuntimeError(f"Plesk recipient identity mapping failed for {address}")
    for domain in batch["domains"]:
        for key in (domain, f"probe@{domain}"):
            expected = forwarding["route"] if ready else RETRY
            if command(["postmap", "-q", key, f"regexp:{MAPS['transport']}"]).strip() != expected:
                raise RuntimeError(f"Plesk transport map has the wrong destination for {key}")
        if command(["postmap", "-q", f"probe@{domain}", f"regexp:{MAPS['sasl']}"]).strip() != DEFER:
            raise RuntimeError(f"Plesk submission block is missing for {domain}")


def forward_source(state, batch, request):
    pending = state["pending"]
    if pending and pending != [batch["domain"], "forward"]:
        raise RuntimeError("Finish the pending source isolation operation before forwarding")
    verify_configuration(state, changing=bool(pending))
    configuration = forwarding_configuration(state)
    forwarding = forwarding_spec(request, batch, configuration)
    current = state["batches"].get(batch["domain"])
    if not current:
        raise RuntimeError("Plesk must remain isolated before forwarding")
    existing = current.get("forwarding")
    if existing and {key: value for key, value in existing.items() if key != "ready"} != forwarding:
        raise RuntimeError("Stored Plesk forwarding plan/destination differs; no routes were changed")
    if state.get("forwardConfig", configuration) != configuration:
        raise RuntimeError("Plesk forwarding configuration changed; retain the source recovery state")
    original_aliases = postconf("virtual_alias_maps")
    if not existing and "virtual_alias_maps" not in state["postfix"]:
        if read(ALIAS_MAP) is not None or str(ALIAS_MAP) in original_aliases:
            raise RuntimeError("Unowned Plesk migration alias map already exists")
    check_passdb()
    check_users(batch)
    check_writers(batch)
    smtp_probe(forwarding["ip"], forwarding_probes(forwarding))
    queue = forwarding_queue(current, forwarding)
    fingerprint = hashlib.sha256(json.dumps([state, configuration, original_aliases, forwarding], sort_keys=True).encode()).hexdigest()
    report = {"stateFile": str(STATE_FILE), "fingerprint": fingerprint, "route": forwarding["route"], "queue": queue,
              "complete": bool(existing and existing.get("ready") and not pending)}
    if request["action"] == "forward-preview":
        return report
    if request.get("fingerprint") != fingerprint:
        raise RuntimeError("Plesk state changed after the forwarding preview; review it again")
    if not existing:
        backup = STATE_DIR / f"before-forward-{batch['domain']}.json"
        canonical = configuration["recipient_canonical_maps"]
        backup_text = json.dumps({"state": state, "virtualAliasMaps": original_aliases,
                                  **({"recipientCanonicalMaps": canonical} if canonical else {})}, indent=2) + "\n"
        saved = read(backup)
        if saved is not None and saved != backup_text:
            raise RuntimeError(f"Forwarding backup differs from its source state: {backup}")
        if saved is None:
            write(backup, backup_text)
        state["priorBatches"] = state["batches"].copy()
        state["postfix"].setdefault("virtual_alias_maps", original_aliases)
        if canonical:
            state["postfix"].setdefault("recipient_canonical_maps", canonical)
        state["forwardConfig"] = configuration
        state["version"] = 2
        state["batches"][batch["domain"]] = {**batch, "forwarding": {**forwarding, "ready": False}}
        state["pending"] = [batch["domain"], "forward"]
        save(state)
    apply(state)
    current = state["batches"][batch["domain"]]
    verify_forwarding_maps(current, forwarding)
    queue = forwarding_queue(current, forwarding)
    smtp_probe(forwarding["ip"], forwarding_probes(forwarding))
    if not current["forwarding"]["ready"]:
        state["priorBatches"] = state["batches"].copy()
        state["batches"][batch["domain"]] = {**batch, "forwarding": {**forwarding, "ready": True}}
        save(state)
        apply(state)
    verify_configuration(state)
    verify_forwarding_maps(state["batches"][batch["domain"]], forwarding)
    smtp_probe("127.0.0.1", forwarding_probes(forwarding))
    check_users(batch)
    state.update({"applied": state["batches"].copy(), "pending": None})
    save(state)
    return {**report, "complete": True, "queue": queue}
