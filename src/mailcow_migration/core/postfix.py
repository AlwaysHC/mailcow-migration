import re
from typing import Mapping, Optional, Tuple

from .errors import MigrationError


def managed_section(text: str, begin: str, end: str) -> Optional[re.Match]:
    if begin not in text and end not in text:
        return None
    section = re.search("^" + re.escape(begin) + "\n(.*?)^" + re.escape(end) + "$", text, re.MULTILINE | re.DOTALL)
    if section is None or text.count(begin) != 1 or text.count(end) != 1:
        raise MigrationError(f"Malformed Postfix configuration section starting with {begin}")
    return section


def update_section(text: str, section: Optional[re.Match], block: str) -> str:
    if section is not None:
        return text[:section.start()] + block + text[section.end():]
    separator = "\n" if text and not text.endswith("\n") else ""
    return text + separator + block + "\n"


def logical_lines(text: Optional[str]) -> Tuple[str, ...]:
    lines = [
        line
        for line in re.split(r"\r?\n", text or "")
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return tuple(re.sub(r"\n[ \t]+", " ", "\n".join(lines)).split("\n"))


def postfix_settings(text: Optional[str]) -> Mapping[str, str]:
    unfolded = re.sub(r"\n[ \t]+", " ", (text or "").replace("\r", ""))
    return {
        match[1]: match[2]
        for line in unfolded.split("\n")
        if (match := re.fullmatch(r"([a-z][a-z0-9_]*)\s*=\s*(.*?)\s*", line))
        is not None
    }
