import re
from typing import Mapping, Optional, Tuple


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
