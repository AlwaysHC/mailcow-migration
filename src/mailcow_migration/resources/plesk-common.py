import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def invoke(arguments, allowed=(0,)):
    result = subprocess.run(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120, check=False)
    if result.stderr:
        sys.stderr.write(result.stderr)
    if result.returncode not in allowed:
        raise RuntimeError(f"{arguments[0]} failed with exit {result.returncode}; see diagnostics above")
    return result


def command(arguments):
    return invoke(arguments).stdout


def query(sql):
    xml = ET.fromstring(command(["plesk", "db", "--xml", "-e", sql]))
    return [{field.attrib["name"]: field.text or "" for field in row} for row in xml.findall("row")]


def mail_root():
    roots = re.findall(r"^PLESK_MAILNAMES_D\s+(\S+)\s*$", Path("/etc/psa/psa.conf").read_text(), re.MULTILINE)
    if len(roots) != 1:
        raise RuntimeError(f"Expected one PLESK_MAILNAMES_D path, found {len(roots)}")
    return roots[0]
