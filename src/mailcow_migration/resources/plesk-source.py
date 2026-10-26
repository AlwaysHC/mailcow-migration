import json
import os


def require_domain(domain):
    if not isinstance(domain, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]+\.[a-z0-9-]+", domain):
        raise RuntimeError("Invalid primary domain")


def inspect(domain):
    require_domain(domain)
    domains = query(f"SELECT id, name, status FROM domains WHERE name='{domain}'")
    aliases = query("SELECT d.name FROM domain_aliases a JOIN domains d ON d.id=a.dom_id "
                    f"WHERE a.name='{domain}' AND a.mail='true'")
    if len(domains) > 1 or len(aliases) > 1 or domains and aliases:
        raise RuntimeError("Ambiguous Plesk primary/alias domain")
    result = {"present": bool(domains), "active": False, "primary": aliases[0]["name"] if aliases else domain,
              "mailRoot": mail_root(), "mailboxes": [], "aliases": []}
    if domains:
        identifier = int(domains[0]["id"])
        services = query(f"SELECT status FROM DomainServices WHERE dom_id={identifier} AND type='mail'")
        if len(services) > 1:
            raise RuntimeError("Ambiguous Plesk mail service")
        result["active"] = bool(services) and int(domains[0]["status"]) == 0 and int(services[0]["status"]) == 0
        result["mailboxes"] = [row["mail_name"] for row in query(
            f"SELECT mail_name FROM mail WHERE dom_id={identifier} AND postbox='true' ORDER BY mail_name")]
        result["aliases"] = [row["name"] for row in query(
            f"SELECT name FROM domain_aliases WHERE dom_id={identifier} AND mail='true' AND status=0 ORDER BY name")]
    return result


def one(rows, label):
    if len(rows) != 1:
        raise RuntimeError(f"Expected one {label}, found {len(rows)}")
    return rows[0]


def boolean(value):
    if value not in ("true", "false", "1", "0"):
        raise RuntimeError("Unsupported Plesk boolean value")
    return value in ("true", "1")


def number(value):
    if not re.fullmatch(r"-?\d+", value):
        raise RuntimeError("Unsupported Plesk numeric value")
    return int(value)


def export(domain):
    require_domain(domain)
    if os.geteuid() != 0:
        raise RuntimeError("Run as root with a lowercase ASCII domain")
    primary = one(query(f"SELECT id, name, status, webspace_id FROM domains WHERE name='{domain}'"), "primary domain")
    domain_id = number(primary["id"])
    service = one(query(f"SELECT * FROM DomainServices WHERE dom_id={domain_id} AND type='mail'"), "mail service")
    parameters_id = number(service["parameters_id"])
    parameters = {row["parameter"]: row["value"] for row in query(f"SELECT * FROM Parameters WHERE id={parameters_id}")}
    subscription_id = number(primary["webspace_id"]) or domain_id
    limits = query("SELECT l.value FROM Subscriptions s JOIN SubscriptionProperties p ON p.subscription_id=s.id "
                   "JOIN Limits l ON l.id=p.value WHERE s.object_type='domain' "
                   f"AND s.object_id={subscription_id} AND p.name='limitsId' AND l.limit_name='mbox_quota'")
    default_quota = number(one(limits, "subscription mailbox quota")["value"])
    root = mail_root()
    mail = query("SELECT id, mail_name, postbox, delivery, account_id, mail_group, autoresponder, "
                 f"mbox_quota, description, externalEmail FROM mail WHERE dom_id={domain_id} ORDER BY id")
    aliases = query("SELECT a.* FROM mail_aliases a JOIN mail m ON m.id=a.mn_id "
                    f"WHERE m.dom_id={domain_id} ORDER BY a.mn_id, a.alias")
    forwards = query("SELECT r.* FROM mail_redir r JOIN mail m ON m.id=r.mn_id "
                     f"WHERE m.dom_id={domain_id} ORDER BY r.mn_id, r.address")
    responses = query("SELECT r.* FROM mail_resp r JOIN mail m ON m.id=r.mn_id "
                      f"WHERE m.dom_id={domain_id} ORDER BY r.id")
    response_forwards = query("SELECT f.* FROM resp_forward f JOIN mail_resp r ON r.id=f.rn_id "
                             f"JOIN mail m ON m.id=r.mn_id WHERE m.dom_id={domain_id} ORDER BY f.id")
    response_attachments = query("SELECT a.* FROM resp_attach a JOIN mail_resp r ON r.id=a.rn_id "
                                f"JOIN mail m ON m.id=r.mn_id WHERE m.dom_id={domain_id} ORDER BY a.id")
    domain_aliases = query(f"SELECT * FROM domain_aliases WHERE dom_id={domain_id} ORDER BY name")
    lists = query(f"SELECT * FROM MailLists WHERE dom_id={domain_id} ORDER BY id")
    credentials = query("SELECT a.id, a.type, a.password FROM accounts a JOIN mail m ON m.account_id=a.id "
                        f"WHERE m.dom_id={domain_id} AND m.postbox='true' ORDER BY a.id")
    auth = command(["plesk", "sbin", "mail_auth_view"]) if any(row["type"] == "sym" for row in credentials) else ""
    blockers = []
    if lists:
        blockers.append("Mailman mailing lists require a separate migration; source records are preserved in the snapshot.")
    if responses:
        blockers.append("Plesk autoresponders require a reviewed Sieve conversion; source records are preserved in the snapshot.")
    accounts = []
    for row in mail:
        local = row["mail_name"]
        if not re.fullmatch(r"[a-z0-9_+.-]+", local) or local in (".", ".."):
            raise RuntimeError("Unsupported mailbox name or casing; no path was guessed")
        username = f"{local}@{domain}"
        home = Path(root) / domain / local
        mailbox = boolean(row["postbox"])
        credential = None
        if mailbox:
            stored = one([item for item in credentials if item["id"] == row["account_id"]], f"credential for {username}")
            if stored["type"] in ("crypt", "plain"):
                credential = {"encrypted": stored["type"] == "crypt", "value": stored["password"]}
            elif stored["type"] == "sym":
                matches = [line.split("|", 3) for line in auth.splitlines() if line.startswith("|")]
                match = one([item for item in matches if len(item) == 4 and item[1].strip() == username],
                            f"mail_auth_view entry for {username}")
                password = match[3].rsplit("|", 1)[0].strip()
                if not password or "..." in password or "\u2026" in password or password.startswith("$AES"):
                    raise RuntimeError(f"Incomplete or encrypted mail_auth_view password for {username}")
                credential = {"encrypted": "E" in match[2], "value": password}
            else:
                raise RuntimeError(f"Unsupported credential type for {username}: {stored['type']}")
        personal = home / ".dovecot.sieve"
        scripts = sorted(set(home.glob("*.sieve")) | set(home.glob("sieve/**/*.sieve")))
        if personal.is_symlink() and not personal.exists():
            raise RuntimeError(f"Broken Sieve link for {username}")
        for script in scripts:
            if script.resolve() != personal.resolve():
                blockers.append(f"{username}: inactive or additional Sieve script needs migration: {script}")
        script = personal.read_text() if personal.exists() else None
        if script is not None and not mailbox:
            blockers.append(f"{username}: Sieve script belongs to an address without a mailbox")
        quota = number(row["mbox_quota"])
        accounts.append({"username": username, "localPart": local, "mailbox": mailbox,
                         "delivery": boolean(row["delivery"]), "forwarding": boolean(row["mail_group"]),
                         "autoresponder": boolean(row["autoresponder"]), "name": row["description"],
                         "recoveryEmail": row["externalEmail"], "quota": max(0, default_quota if quota == -1 else quota),
                         "credential": credential, "script": script,
                         "aliases": [item["alias"] for item in aliases if item["mn_id"] == row["id"]],
                         "destinations": [item["address"] for item in forwards if item["mn_id"] == row["id"]]})
    return {"version": 1, "domain": domain, "mailRoot": root,
            "active": number(primary["status"]) == 0 and number(service["status"]) == 0,
            "parameters": parameters, "accounts": accounts, "aliasDomains": domain_aliases,
            "autoresponders": responses, "responseForwards": response_forwards, "responseAttachments": response_attachments,
            "mailingLists": lists, "blockers": blockers}


if __name__ == "__main__":
    try:
        request = json.load(sys.stdin)
        actions = {"inspect": inspect, "export": export}
        print(json.dumps(actions[request["action"]](request["domain"]), ensure_ascii=True))
    except Exception as error:
        print(f"Plesk source operation stopped: {error}", file=sys.stderr)
        sys.exit(1)
