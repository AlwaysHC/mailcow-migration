import json
import logging
import os
import re
import stat
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

STATE_DIR = Path("/root/mailcow-in-progress/plesk-isolation")
STATE_FILE = STATE_DIR / "state.json"
DOVECOT = Path("/etc/dovecot/dovecot.conf")
DENY = Path("/etc/dovecot/mailcow-plesk-deny")
MAPS = {
    name: Path(f"/etc/postfix/mailcow-plesk-{name}")
    for name in ("recipients", "sasl", "transport")
}
ALIAS_MAP = Path("/etc/postfix/mailcow-plesk-aliases")
RETRY = "retry:4.3.2 Domain migration in progress"
DEFER = "451 4.3.2 Domain migration in progress"
PREFIXES = {
    "transport_maps": f"regexp:{MAPS['transport']}",
    "smtpd_sender_restrictions": f"check_sasl_access regexp:{MAPS['sasl']}",
    "smtpd_recipient_restrictions": f"check_recipient_access regexp:{MAPS['recipients']}",
}
BLOCK = (
    "\n# BEGIN Mailcow Plesk isolation\npassdb mailcow_migration {\n"
    f"  driver = passwd-file\n  passwd_file_path = {DENY}\n"
    "  deny = yes\n  result_internalfail = return-fail\n}\n# END Mailcow Plesk isolation\n"
)


logger = logging.getLogger(__name__)


def invoke(arguments, allowed=(0,)):
    result = subprocess.run(
        arguments,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.stderr:
        sys.stderr.write(result.stderr)
    if result.returncode not in allowed:
        raise RuntimeError(
            f"{arguments[0]} failed with exit {result.returncode}; see diagnostics above"
        )
    return result


def command(arguments):
    return invoke(arguments).stdout


def query(sql):
    xml = ET.fromstring(command(["plesk", "db", "--xml", "-e", sql]))
    return [
        {field.attrib["name"]: field.text or "" for field in row}
        for row in xml.findall("row")
    ]


def mail_root():
    roots = re.findall(
        r"^PLESK_MAILNAMES_D\s+(\S+)\s*$",
        Path("/etc/psa/psa.conf").read_text(),
        re.MULTILINE,
    )
    if len(roots) != 1:
        raise RuntimeError(f"Expected one PLESK_MAILNAMES_D path, found {len(roots)}")
    return roots[0]


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


def managed(name, original):
    prefix = (
        f"regexp:{ALIAS_MAP}"
        if name in ("virtual_alias_maps", "recipient_canonical_maps")
        else PREFIXES[name.rsplit("/", 1)[-1]]
    )
    return prefix + (", " + original if original else "")


def run_request(operation, failure_message):
    try:
        print(json.dumps(operation(json.load(sys.stdin)), ensure_ascii=True))
    except Exception:
        logger.exception(failure_message)
        sys.exit(1)
