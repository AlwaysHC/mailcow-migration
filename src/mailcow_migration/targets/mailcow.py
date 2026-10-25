import errno
import json
import logging
import re
import socket
import ssl
import time
from dataclasses import dataclass
from http.client import HTTPException
from pathlib import Path
from typing import Any, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from ..configuration import MailcowConfiguration, api_origin
from ..core.commands import CommandResult, run_command
from ..core.errors import ApiError, ConfigurationError
from ..core.storage import decode_json, read_text

LOGGER = logging.getLogger("mailcow_migration")
READ = object()
TRANSIENT_CODES = {errno.ECONNRESET, errno.ECONNREFUSED, errno.EPIPE, errno.ETIMEDOUT, socket.EAI_AGAIN}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


@dataclass(frozen=True)
class MailcowApi:
    origin: str
    key_file: Path
    timeout: float = 60

    def __post_init__(self) -> None:
        api_origin(self.origin)
        if self.timeout <= 0:
            raise ConfigurationError("Mailcow API timeout must be positive")

    def inventory(self, kind: str) -> list:
        rows = self.request("get/{}/all".format(kind))
        if any(not isinstance(row, dict) for row in rows):
            raise ApiError("Invalid Mailcow inventory; contents omitted")
        return rows

    def request(self, endpoint: str, payload: Any = READ, *, object_response: bool = False) -> Any:
        if not re.fullmatch(r"[a-zA-Z0-9_@.+%/-]+", endpoint) or endpoint.startswith("/") or ".." in endpoint.split("/"):
            raise ConfigurationError("Invalid Mailcow API endpoint")
        key = read_text(self.key_file, private=True).strip()
        if not key or any(char.isspace() or ord(char) < 33 or ord(char) > 126 for char in key):
            raise ConfigurationError("Invalid API key file; expected a single ASCII key")
        method = "GET" if payload is READ else "POST"
        body = None if payload is READ else json.dumps(payload, ensure_ascii=True, allow_nan=False).encode("utf-8")
        request = Request(
            self.origin.rstrip("/") + "/api/v1/" + endpoint, data=body, method=method,
            headers={"X-API-Key": key, "Content-Type": "application/json"},
        )
        opener = build_opener(NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))
        attempt = 1
        while True:
            try:
                with opener.open(request, timeout=self.timeout) as response:
                    result = decode_json(response.read().decode("utf-8"))
                break
            except HTTPError as error:
                failure = ApiError(
                    "Mailcow API {} returned HTTP {}{}".format(
                        method, error.code, "; check API key permissions and allowed source IP" if error.code in (401, 403) else ""
                    ), uncertain=method == "POST", transient=error.code in (502, 503, 504),
                )
                error.close()
            except (ValueError, UnicodeError):
                failure = ApiError("Mailcow API returned invalid JSON; body omitted", uncertain=method == "POST")
            except (URLError, OSError, HTTPException) as error:
                reason = error.reason if isinstance(error, URLError) else error
                failure = ApiError(
                    "Mailcow API connection failed ({})".format(type(reason).__name__), uncertain=method == "POST",
                    transient=isinstance(reason, (TimeoutError, ConnectionError, HTTPException))
                    or getattr(reason, "errno", None) in TRANSIENT_CODES,
                )
            if failure.uncertain:
                raise ApiError(
                    "{}. The write may have reached Mailcow; reconcile the saved journal before retrying".format(failure), uncertain=True
                ) from failure
            if not failure.transient or attempt == 3:
                raise failure
            LOGGER.warning("%s; retrying read in %s second(s), attempt %s/3", failure, attempt, attempt + 1)
            time.sleep(attempt)
            attempt += 1
        if payload is not READ:
            entries = result if isinstance(result, list) else [result]
            if not entries or any(not isinstance(item, dict) or item.get("type") != "success" for item in entries):
                codes = []
                for item in entries:
                    if isinstance(item, dict) and item.get("type") != "success":
                        message = item.get("msg")
                        code = message[0] if isinstance(message, list) and message else message
                        codes.append(code if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_-]*", code, re.I) else "rejected")
                raise ApiError(
                    "Mailcow API did not report success ({}); retain the journal and inspect the target".format(
                        ", ".join(codes) or "empty or invalid response"
                    ), uncertain=True,
                )
        elif not isinstance(result, list):
            if object_response and isinstance(result, dict):
                return result
            if endpoint.endswith("/all") and result == {}:
                return []
            raise ApiError("Unexpected Mailcow inventory response; body omitted")
        return result


@dataclass(frozen=True)
class MailcowTarget:
    settings: MailcowConfiguration

    @property
    def api(self) -> MailcowApi:
        return MailcowApi(self.settings.api, Path(self.settings.api_key_file))

    def compose(
        self, arguments: Sequence[str], *, input_text: Optional[str] = None, timeout: float = 120, private_output: bool = True,
        diagnostic_file: Optional[Path] = None, allowed_codes: Sequence[int] = (0,),
    ) -> CommandResult:
        return run_command(
            ("docker", "compose", "--project-directory", self.settings.directory, *arguments),
            input_text=input_text, timeout=timeout, private_output=private_output, label="Mailcow Docker Compose",
            diagnostic_file=diagnostic_file, allowed_codes=allowed_codes,
        )
