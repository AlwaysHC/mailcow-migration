import fcntl
import json
import os
import re
import stat
import tempfile
import time
from pathlib import Path

STATE_DIR = Path("/root/mailcow-in-progress/plesk-isolation")
STATE_FILE = STATE_DIR / "state.json"
DOVECOT = Path("/etc/dovecot/dovecot.conf")
DENY = Path("/etc/dovecot/mailcow-plesk-deny")
MAPS = {name: Path(f"/etc/postfix/mailcow-plesk-{name}") for name in ("recipients", "sasl", "transport")}
ALIAS_MAP = Path("/etc/postfix/mailcow-plesk-aliases")
RETRY = "retry:4.3.2 Domain migration in progress"
DEFER = "451 4.3.2 Domain migration in progress"
PREFIXES = {"transport_maps": f"regexp:{MAPS['transport']}",
            "smtpd_sender_restrictions": f"check_sasl_access regexp:{MAPS['sasl']}",
            "smtpd_recipient_restrictions": f"check_recipient_access regexp:{MAPS['recipients']}"}
BLOCK = ("\n# BEGIN Mailcow Plesk isolation\npassdb mailcow_migration {\n"
         f"  driver = passwd-file\n  passwd_file_path = {DENY}\n"
         "  deny = yes\n  result_internalfail = return-fail\n}\n# END Mailcow Plesk isolation\n")


def read(file):
    for ancestor in (file, *file.parents):
        if ancestor.is_symlink():
            raise RuntimeError(f"Symlinked isolation path: {ancestor}")
    if not file.exists():
        return None
    info = file.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise RuntimeError(f"Isolation path is not a regular single-link file: {file}")
    return file.read_text()


def write(file, text, mode=0o600):
    read(file)
    descriptor, temporary = tempfile.mkstemp(prefix=".mailcow-", dir=file.parent)
    with os.fdopen(descriptor, "w") as stream:
        os.fchmod(stream.fileno(), mode)
        if file.exists():
            info = file.stat()
            os.fchown(stream.fileno(), info.st_uid, info.st_gid)
            os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode))
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, file)


def save(state):
    write(STATE_FILE, json.dumps(state, indent=2) + "\n")


def postconf(name):
    return command(["postconf", "-h", name]).strip()


def overrides():
    result = {}
    for line in command(["postconf", "-P"]).splitlines():
        key, separator, value = line.partition("=")
        if not separator:
            raise RuntimeError("Cannot parse Postfix master.cf overrides")
        name = key.rsplit("/", 1)[-1]
        if name == "transport_maps":
            raise RuntimeError("A Postfix service overrides transport_maps; a delivery adapter is required")
        if name in PREFIXES:
            result[key] = value.strip()
    return result


def managed(name, original):
    prefix = f"regexp:{ALIAS_MAP}" if name in ("virtual_alias_maps", "recipient_canonical_maps") else PREFIXES[name.rsplit("/", 1)[-1]]
    return prefix + (", " + original if original else "")


def preflight():
    version = command(["dovecot", "--version"]).strip()
    if not version.startswith("2.4."):
        raise RuntimeError("Automatic Plesk isolation requires Dovecot 2.4")
    if command(["doveconf", "-h", "auth_username_format"]).strip() != "%{user | lower}":
        raise RuntimeError("Unsupported Dovecot username rewriting")
    if command(["doveconf", "-h", "auth_default_realm"]).strip():
        raise RuntimeError("A Dovecot default realm needs a short-login isolation adapter")
    if command(["doveconf", "-h", "auth_master_user_separator"]).strip():
        raise RuntimeError("Dovecot master logins need a separate isolation adapter")
    options = command(["plesk", "bin", "mailserver", "--info", "options"])
    if not re.search(r"^Short POP3/IMAP mail account names:\s*off\s*$", options, re.MULTILINE):
        raise RuntimeError("Plesk short logins must be disabled before per-domain isolation")
    smtp_version = postconf("mail_version")
    if not re.match(r"[3-9]\.", smtp_version) or postconf("smtpd_sasl_type") not in ("cyrus", "dovecot"):
        raise RuntimeError("Unsupported Postfix version or SASL backend")
    if "regexp" not in command(["postconf", "-m"]).split():
        raise RuntimeError("Postfix regexp maps are unavailable")
    master = command(["postconf", "-M"])
    if not re.search(r"^retry\s+unix\s+.*\berror\s*$", master, re.MULTILINE):
        raise RuntimeError("Postfix retry/error transport is missing")
    return {"dovecot": version, "postfix": smtp_version, "sasl": postconf("smtpd_sasl_type")}


def logins(batch):
    return sorted({f"{user.split('@')[0]}@{domain}" for user in batch["users"] for domain in batch["domains"]})


def files(batches):
    domains = sorted({domain for batch in batches.values() for domain in batch["domains"]})
    held = sorted({domain for batch in batches.values() if not batch.get("forwarding", {}).get("ready") for domain in batch["domains"]})
    users = sorted({user for batch in batches.values() for user in logins(batch)})
    pattern = "|".join(re.escape(domain) for domain in domains)
    sasl = f"/^[^@]+@({pattern})$/ {DEFER}\n" if domains else ""
    pattern = "|".join(re.escape(domain) for domain in held)
    recipient = f"/^[^@]+@({pattern})$/ {DEFER}\n" if held else ""
    transport = f"/^([^@]+@)?({pattern})$/ {RETRY}\n" if held else ""
    aliases = ""
    forwarded = [batch["forwarding"] for _, batch in sorted(batches.items()) if "forwarding" in batch]
    for forwarding in forwarded:
        access, identities = forwarding_rules(forwarding)
        aliases += identities
        if forwarding["ready"]:
            recipient += access
            pattern = "|".join(re.escape(row["domain"]) for row in forwarding["policies"])
            transport += f"/^([^@]+@)?({pattern})$/ {forwarding['route']}\n"
    return {DENY: "".join(user + "\n" for user in users),
            MAPS["recipients"]: recipient, MAPS["sasl"]: sasl, MAPS["transport"]: transport,
            **({ALIAS_MAP: aliases} if forwarded else {})}


def initial_state():
    original = read(DOVECOT)
    matches = list(re.finditer(r"^dovecot_config_version\s*=\s*2\.4\.[0-9]+\s*$", original or "", re.MULTILINE))
    if len(matches) != 1 or "mailcow_migration" in original:
        raise RuntimeError("Cannot place the migration passdb after Dovecot's config-version header")
    if re.search(r"^[ \t]*(?:!include|passdb\b|protocol\b)", original[:matches[0].start()], re.MULTILINE):
        raise RuntimeError("Authentication configuration precedes Dovecot's version header")
    for file in files({}):
        if read(file) is not None:
            raise RuntimeError(f"Unowned isolation file already exists: {file}")
    base = {name: postconf(name) for name in PREFIXES}
    master = overrides()
    if any("mailcow-plesk-" in value for value in [*base.values(), *master.values()]):
        raise RuntimeError("Unowned Postfix isolation configuration already exists")
    return {"version": 1, "batches": {}, "applied": {}, "originalDovecot": original,
            "managedDovecot": original[:matches[0].end()] + BLOCK + original[matches[0].end():],
            "postfix": base, "master": master, "initialized": False, "pending": None, "priorBatches": {}, "draining": {}}


def verify_configuration(state, changing=False):
    pending = bool(state["pending"]) and changing
    active = bool(state["batches"])
    allowed = [state["managedDovecot"] if active else state["originalDovecot"]]
    if pending:
        allowed.extend([state["originalDovecot"], state["managedDovecot"]])
    if read(DOVECOT) not in allowed:
        raise RuntimeError("Dovecot configuration changed outside isolation; retain the source recovery state")
    actual_master = overrides()
    if actual_master.keys() != state["master"].keys():
        raise RuntimeError("Postfix service overrides changed outside isolation")
    if "forwardConfig" in state and forwarding_configuration(state) != state["forwardConfig"]:
        raise RuntimeError("Plesk forwarding configuration changed outside migration")
    for name, original in {**state["postfix"], **state["master"]}.items():
        actual = actual_master[name] if "/" in name else postconf(name)
        allowed = [managed(name, original) if active else original]
        if pending:
            allowed.extend([original, managed(name, original)])
        if actual not in allowed:
            raise RuntimeError(f"Postfix isolation setting changed: {name}")
    previous = files(state["applied"])
    interrupted = files(state["priorBatches"])
    for file, desired in files(state["batches"]).items():
        allowed = [desired]
        if pending:
            allowed.append(previous.get(file))
            allowed.append(interrupted.get(file))
            if not state["initialized"]:
                allowed.append(None)
        if read(file) not in allowed:
            raise RuntimeError(f"Isolation file changed outside migration: {file}")


def processes():
    result = {}
    for file in Path("/proc").glob("[0-9]*/stat"):
        try:
            value = file.read_text()
        except FileNotFoundError:
            continue
        name = value.split("(", 1)[1].rsplit(")", 1)[0]
        if name in {"smtpd", "cleanup", "qmgr", "pipe", "local", "virtual", "lmtp", "dovecot-lda", "deliver", "postfix-local"}:
            result[file.parent.name] = value.rsplit(")", 1)[1].split()[19]
    return result


def drain(state):
    if not state["draining"]:
        return
    for attempt in range(40):
        current = processes()
        remaining = {pid: started for pid, started in state["draining"].items() if current.get(pid) == started}
        if not remaining:
            state["draining"] = {}
            save(state)
            return
        time.sleep(0.25)
    raise RuntimeError("Old Postfix workers/sessions remain: " + ", ".join(remaining) + ". Holds remain installed; rerun to resume")


def check_users(batch, disconnect=False):
    for user in logins(batch):
        if disconnect:
            command(["doveadm", "auth", "cache", "flush", user])
            result = invoke(["doveadm", "kick", user], allowed=(0, 68))
            if result.returncode == 68 and (result.stdout.strip() != "no users kicked" or result.stderr.strip()):
                raise RuntimeError(f"Unexpected Dovecot disconnect result for {user}")
        for protocol in ("imap", "pop3", "sieve"):
            result = invoke(["doveadm", "auth", "lookup", "-x", f"service={protocol}", "-f", "user", user], allowed=(0, 67))
            if result.returncode != 67 or result.stdout.strip():
                raise RuntimeError(f"Source authentication is not blocked for {user} ({protocol})")
        if command(["doveadm", "-f", "flow", "who", "-1", user]).strip():
            raise RuntimeError(f"Dovecot sessions remain for {user}; rerun after they close")


def check_writers(batch, open_files=True):
    root = Path(batch["root"]) / batch["domain"]
    for directory in Path("/proc").glob("[0-9]*/fd") if open_files else ():
        for file in directory.glob("*"):
            try:
                target = os.readlink(file)
                if not target.startswith(str(root) + "/"):
                    continue
                info = (directory.parent / "fdinfo" / file.name).read_text()
            except FileNotFoundError:
                continue
            flags = re.search(r"^flags:\s+([0-7]+)$", info, re.MULTILINE)
            if not flags or int(flags[1], 8) & 3:
                raise RuntimeError(f"Open writable mailbox file: PID {directory.parent.name}: {target}")
    cron = [Path("/etc/crontab"), *Path("/etc/cron.d").glob("*"), *Path("/var/spool/cron/crontabs").glob("*")]
    for file in cron:
        if not file.is_file():
            continue
        for index, line in enumerate(file.read_text().splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if any(domain in line for domain in batch["domains"]) and re.search(r"\b(fetchmail|getmail|imapsync|offlineimap|mbsync|dsync)\b", line):
                raise RuntimeError(f"Pause the batch mail-sync job at {file}:{index}, then rerun")


def check_passdb(configuration=DOVECOT):
    output = command(["doveconf", "-c", str(configuration), "-n", "passdb"])
    first = re.search(r"^passdb[ \t]+(\S+)[ \t]+\{[ \t]*$", output, re.MULTILINE)
    if not first or first[1] != "mailcow_migration":
        raise RuntimeError("The migration deny passdb is not first in Dovecot's authentication chain")


def check(state, batch):
    verify_configuration(state)
    check_passdb()
    drain(state)
    for domain in batch["domains"]:
        for name, key, expected in (("recipients", f"probe@{domain}", DEFER), ("sasl", f"probe@{domain}", DEFER),
                                    ("transport", domain, RETRY), ("transport", f"probe@{domain}", RETRY)):
            if command(["postmap", "-q", key, f"regexp:{MAPS[name]}"]).strip() != expected:
                raise RuntimeError(f"Postfix {name} map does not block {domain}")
    check_users(batch)
    check_writers(batch)


def apply(state):
    verify_configuration(state, changing=True)
    active = bool(state["batches"])
    configuration = state["managedDovecot"] if active else state["originalDovecot"]
    for file, text in files(state["batches"]).items():
        write(file, text, 0o644)
    descriptor, candidate = tempfile.mkstemp(prefix=".mailcow-preview-", dir=DOVECOT.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(configuration)
        command(["doveconf", "-c", candidate, "-n"])
        if active:
            check_passdb(candidate)
    finally:
        os.unlink(candidate)
    write(DOVECOT, configuration)
    for name, original in {**state["postfix"], **state["master"]}.items():
        command(["postconf", "-P" if "/" in name else "-e", name + "=" + (managed(name, original) if active else original)])
    command(["postfix", "check"])
    state["draining"] = {**processes(), **state["draining"]}
    save(state)
    command(["postfix", "reload"])
    command(["doveadm", "reload"])
    drain(state)


def main(request):
    if os.geteuid() != 0 or request["action"] not in ("inspect", "freeze", "check", "check-webmail", "release", "forward-preview", "forward"):
        raise RuntimeError("Run a supported isolation action as root")
    domains = request["domains"]
    if not domains or request["domain"] not in domains or any(not re.fullmatch(r"[a-z0-9][a-z0-9.-]+\.[a-z0-9-]+", d) for d in domains):
        raise RuntimeError("Invalid isolation domains")
    if any(not re.fullmatch(r"[a-z0-9_+.-]+@" + re.escape(request["domain"]), user) for user in request["users"]):
        raise RuntimeError("Invalid isolation usernames")
    if not re.fullmatch(r"/[a-zA-Z0-9_/-]+", request["root"]):
        raise RuntimeError("Invalid source mail root")
    environment = preflight()
    stored = read(STATE_FILE)
    state = json.loads(stored) if stored else initial_state()
    if state.get("version") not in (1, 2):
        raise RuntimeError("Unsupported source isolation state version")
    batch = {key: request[key] for key in ("domain", "domains", "users", "root", "identity")}
    previous = (state["batches"].get(request["domain"]) or state["applied"].get(request["domain"])
                or (state["priorBatches"].get(request["domain"]) if state["pending"] else None))
    if previous and {key: value for key, value in previous.items() if key != "forwarding"} != batch:
        raise RuntimeError("Isolation belongs to another source plan")
    if any(set(other["domains"]) & set(domains) for name, other in state["batches"].items() if name != request["domain"]):
        raise RuntimeError("Isolation domains overlap another migration batch")
    result = {**environment, "stateFile": str(STATE_FILE), "blocked": bool(previous)}
    if request["action"] in ("forward-preview", "forward"):
        return {**result, **forward_source(state, batch, request)}
    if previous and "forwarding" in previous and request["action"] not in ("inspect", "check-webmail"):
        raise RuntimeError("Plesk forwarding has started; source release, refreeze and copying are forbidden. Resume the handoff")
    if request["action"] == "inspect":
        if state["initialized"]:
            verify_configuration(state, changing=True)
        check_writers(batch, open_files=False)
        return result
    if request["action"] == "check-webmail":
        if not previous or state["pending"]:
            raise RuntimeError("Plesk login isolation is absent or incomplete; finish source isolation before webmail import")
        verify_configuration(state)
        check_passdb()
        check_users(batch)
        return result
    if request["action"] == "check":
        if not previous or state["pending"]:
            raise RuntimeError("Plesk isolation is absent or incomplete; rerun the freeze step")
        check(state, batch)
        return result
    pending = state["pending"]
    if pending and (pending[0] != request["domain"] or pending[1] in ("release", "forward") and request["action"] != pending[1]):
        raise RuntimeError("Finish the interrupted source isolation change, or release that same batch before starting another")
    if request["action"] == "release" and not previous and not state["pending"]:
        return result
    if not pending or pending[1] != request["action"]:
        state["priorBatches"] = state["batches"].copy()
    if request["action"] == "freeze":
        state["batches"][request["domain"]] = batch
    else:
        state["batches"].pop(request["domain"], None)
    state["pending"] = [request["domain"], request["action"]]
    save(state)
    apply(state)
    if request["action"] == "freeze":
        check_users(batch, disconnect=True)
        check(state, batch)
    else:
        for user in logins(batch):
            command(["doveadm", "auth", "cache", "flush", user])
        verify_configuration(state)
    state.update({"applied": state["batches"].copy(), "pending": None, "initialized": bool(state["batches"])})
    save(state)
    return {**result, "blocked": request["action"] == "freeze"}


try:
    request = json.load(sys.stdin)
    if request.get("action") in ("freeze", "release", "check", "check-webmail") or STATE_FILE.exists():
        STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        read(STATE_FILE)
        lock_file = STATE_DIR / "operation.lock"
        read(lock_file)
        with lock_file.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            print(json.dumps(main(request)))
    else:
        print(json.dumps(main(request)))
except Exception as error:
    print(f"Plesk isolation stopped: {error}\nSource recovery state: {STATE_FILE}. A pending change may be partly applied.", file=sys.stderr)
    sys.exit(1)
