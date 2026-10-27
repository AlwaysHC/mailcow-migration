import re
from dataclasses import dataclass
from typing import Any, Mapping, NoReturn, Optional, Sequence, Tuple

from ..core.errors import MigrationError

SPACE = r"[\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]"
TOKEN = re.compile(
    SPACE + r'+|#[^\r\n]*|"(?:[^"\\\r\n]|\\["\\])*"|:[a-zA-Z][a-zA-Z0-9_-]*|'
    r"[a-zA-Z][a-zA-Z0-9_]*|[0-9]+[KMG]?|[\[\](){},;]"
)
FLAGS = {
    "\\Answered": "answered",
    "\\Deleted": "deleted",
    "\\Draft": "draft",
    "\\Flagged": "flagged",
    "\\Seen": "seen",
    "Junk": "junk",
    "NotJunk": "not_junk",
}


class SieveMappingError(MigrationError):
    pass


@dataclass(frozen=True)
class Token:
    kind: str
    value: str


def tokenize(source: str) -> Tuple[Token, ...]:
    tokens, offset = [], 0
    while offset < len(source):
        match = TOKEN.match(source, offset)
        if match is None:
            raise SieveMappingError(
                "Unsupported Sieve syntax at line {}".format(
                    source.count("\n", 0, offset) + 1
                )
            )
        text = match[0]
        name = re.fullmatch(
            "#" + SPACE + r"*rule:\[([^\r\n\u2028\u2029]*)\]" + SPACE + "*", text
        )
        if name:
            tokens.append(Token("name", name[1]))
        elif not re.match(SPACE + "|#", text):
            tokens.append(
                Token("string", re.sub(r'\\(["\\])', r"\1", text[1:-1]))
                if text.startswith('"')
                else Token("word", text.lower())
            )
        offset = match.end()
    return tuple(tokens)


class SieveParser:
    def __init__(self, tokens: Sequence[Token]) -> None:
        self.tokens = tokens
        self.position = 0

    def current(self) -> Optional[Token]:
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def peek(self, value: str) -> bool:
        token = self.current()
        return token is not None and token.kind == "word" and token.value == value

    def fail(self, message: str) -> NoReturn:
        token = self.current()
        detail = (
            "end of script"
            if token is None
            else token.value
            if token.kind == "word"
            else token.kind
        )
        raise SieveMappingError(f"{message} near token {self.position + 1} ({detail})")

    def take(self, value: str) -> None:
        if not self.peek(value):
            self.fail("Expected " + value)
        self.position += 1

    def value(self) -> Optional[str]:
        token = self.current()
        self.position += 1
        return token.value if token is not None else None

    def string(self) -> str:
        token = self.current()
        if token is None or token.kind != "string":
            self.fail(
                "Expected a quoted string; string lists and multiline text need review"
            )
        self.position += 1
        if re.search(r"[\x00-\x1f\x7f]", token.value):
            self.fail("Control characters cannot be mapped to editable filters")
        return token.value

    def header(self, value: str) -> Mapping[str, str]:
        if not re.fullmatch(r"[!-9;-~]+", value):
            self.fail("Invalid header name")
        return {"field": "header", "custom_header": value}

    def condition(self) -> Mapping[str, Any]:
        negative = self.peek("not")
        if negative:
            self.take("not")
        kind = self.value()
        if kind == "exists":
            return {
                **self.header(self.string()),
                "operator": "matches_not" if negative else "matches",
                "value": "*",
            }
        if kind not in ("header", "address", "size"):
            self.fail("Unsupported Sieve condition")
        if kind == "size":
            operator = (self.value() or "")[1:]
            match = re.fullmatch(r"([0-9]+)([kmg]?)", self.value() or "")
            digits = (match[1].lstrip("0") or "0") if match else "0"
            value = (
                int(digits)
                * {"": 1, "k": 1024, "m": 1048576, "g": 1073741824}[match[2]]
                if match and len(digits) <= 13
                else 0
            )
            if (
                negative
                or operator not in ("over", "under")
                or value % 1024
                or not 1 <= value // 1024 <= 2147483647
            ):
                self.fail(
                    "Size conditions require positive whole KiB and a non-negated comparison"
                )
            return {"field": "size", "operator": operator, "value": value // 1024}
        operator, matched, address_part = "is", False, False
        while (
            (token := self.current()) is not None
            and token.kind == "word"
            and token.value.startswith(":")
        ):
            tag = token.value
            self.position += 1
            if tag in (":is", ":contains", ":matches") and not matched:
                operator, matched = tag[1:], True
            elif tag == ":all" and kind == "address" and not address_part:
                address_part = True
            else:
                self.fail(f"Unsupported or repeated condition modifier {tag}")
        name, value = self.string(), self.string()
        if not value:
            self.fail("Empty comparison values cannot be edited safely in SOGo")
        if kind == "address" and name.lower() not in ("to", "cc", "from"):
            self.fail("SOGo only exposes To, Cc and From address conditions")
        return {
            **(self.header(name) if kind == "header" else {"field": name.lower()}),
            "operator": operator + ("_not" if negative else ""),
            "value": value,
        }

    def actions(self) -> list:
        actions = []
        while not self.peek("}"):
            method = self.value()
            if method in ("stop", "keep", "discard"):
                actions.append({"method": method})
            elif method in ("fileinto", "redirect", "reject", "addflag"):
                argument = self.string()
                if not argument:
                    self.fail("Empty action argument")
                if method == "fileinto" and re.search(r"[./]|&[^-]*-", argument):
                    self.fail(
                        "Folder hierarchy or modified UTF-7 needs an explicit SOGo folder mapping"
                    )
                if method == "redirect" and not re.fullmatch(
                    r"[^\s@,<>]+@[^\s@,<>]+", argument
                ):
                    self.fail("Unsupported redirect address")
                if method == "addflag" and argument not in FLAGS:
                    self.fail("Unsupported flag")
                actions.append(
                    {
                        "method": method,
                        "argument": FLAGS[argument]
                        if method == "addflag"
                        else argument,
                    }
                )
            else:
                self.fail("Unsupported Sieve action")
            self.take(";")
        if not actions:
            self.fail("Empty action block")
        return actions

    def filters(self) -> list:
        filters, name = [], None
        while (token := self.current()) is not None:
            if token.kind == "name":
                name = self.value()
                continue
            if self.peek("require") and not filters:
                self.take("require")
                listed = self.peek("[")
                if listed:
                    self.take("[")
                while True:
                    extension = self.string()
                    if extension not in (
                        "fileinto",
                        "reject",
                        "imap4flags",
                        "imapflags",
                    ):
                        self.fail("Unsupported Sieve extension")
                    if not listed or not self.peek(","):
                        break
                    self.take(",")
                if listed:
                    self.take("]")
                self.take(";")
                continue
            self.take("if")
            rules, match = [], "all"
            if self.peek("true"):
                self.take("true")
                match = "allmessages"
            elif self.peek("allof") or self.peek("anyof"):
                match = "all" if self.value() == "allof" else "any"
                self.take("(")
                rules.append(self.condition())
                while self.peek(","):
                    self.take(",")
                    rules.append(self.condition())
                self.take(")")
            else:
                rules.append(self.condition())
            self.take("{")
            actions = self.actions()
            self.take("}")
            filters.append(
                {
                    "name": name or f"Imported rule {len(filters) + 1}",
                    "active": 1,
                    "match": match,
                    "rules": rules,
                    "actions": actions,
                }
            )
            name = None
        return filters


def sieve_filters(source: str) -> list:
    return SieveParser(tokenize(source)).filters()


def requires_forwarding_prefilter(filters: Sequence[Mapping[str, Any]]) -> bool:
    return any(
        action["method"] not in ("keep", "stop")
        for rule in filters
        for action in rule["actions"]
    )
