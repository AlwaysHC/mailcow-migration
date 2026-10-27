import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Optional, Tuple, Type, TypeVar
from urllib.parse import urlsplit

from .core.errors import ConfigurationError, MigrationError
from .core.storage import read_json

WORK_ROOT = "/root/mailcow-in-progress"
CONFIGURATION_VERSION = 1
Section = TypeVar("Section")


def text_value(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ConfigurationError("Enter a nonempty value without control characters")
    return value


def ssh_host(value: Any) -> str:
    host = text_value(value)
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.@-]*", host):
        raise ConfigurationError(
            "Use an SSH hostname or IPv4 address, optionally preceded by user@"
        )
    return host


def absolute_path(value: Any) -> str:
    path = text_value(value)
    if not path.startswith("/") or ".." in PurePosixPath(path).parts:
        raise ConfigurationError(
            "Use an absolute Linux path without parent-directory traversal"
        )
    return path


def optional_path(value: Any) -> Optional[str]:
    return None if value is None else absolute_path(value)


def mail_root(value: Any) -> str:
    path = absolute_path(value).rstrip("/")
    if not re.fullmatch(r"/[a-zA-Z0-9_/-]+", path):
        raise ConfigurationError("Use a simple absolute source mail directory")
    return path


def api_origin(value: Any) -> str:
    origin = text_value(value)
    try:
        url = urlsplit(origin)
        port = url.port
    except ValueError as error:
        raise ConfigurationError("Use a valid HTTPS Mailcow origin") from error
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.path not in ("", "/")
        or url.query
        or url.fragment
        or "?" in origin
        or "#" in origin
        or any(char.isspace() for char in origin)
        or "\\" in origin
        or port == 0
    ):
        raise ConfigurationError(
            "Use an HTTPS origin without credentials, path, query, or fragment"
        )
    return origin.rstrip("/")


def bounded_integer(value: Any, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ConfigurationError(f"Enter an integer from {minimum} to {maximum}")
    return value


def choice_value(value: Any, choices: Tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ConfigurationError("Choose one of: " + ", ".join(choices))
    return value


def option(label: str, default: Any, validate: Callable[[Any], Any]) -> Any:
    return field(default=default, metadata={"label": label, "validate": validate})


@dataclass(frozen=True)
class SourceConfiguration:
    source: str = option("Source SSH host", "", ssh_host)
    ssh_config: Optional[str] = option(
        "SSH configuration file on this host (optional)", None, optional_path
    )


@dataclass(frozen=True)
class PostfixAdminConfiguration(SourceConfiguration):
    source_mail_root: str = option("PostfixAdmin mail root", "/var/posta", mail_root)
    mysql_option_file: str = option(
        "MySQL credentials file on the source",
        "/root/migration-mysql.cnf",
        absolute_path,
    )
    roundcube_directory: Optional[str] = option(
        "RoundCube directory on the source (optional)", None, optional_path
    )


@dataclass(frozen=True)
class PleskConfiguration(SourceConfiguration):
    roundcube_directory: str = option(
        "Plesk RoundCube directory", "/usr/share/psa-roundcube", absolute_path
    )


@dataclass(frozen=True)
class MailcowConfiguration:
    directory: str = option(
        "Mailcow installation directory", "/opt/mailcow-dockerized", absolute_path
    )
    api: str = option("Mailcow API origin", "", api_origin)
    api_key_file: str = option(
        "Mailcow API key file",
        WORK_ROOT + "/credentials/mailcow-api.key",
        absolute_path,
    )


@dataclass(frozen=True)
class MigrationConfiguration:
    quota_headroom_percent: int = option(
        "Quota headroom percent", 20, lambda value: bounded_integer(value, 0, 1000)
    )
    quota_min_mib: int = option(
        "Minimum mailbox quota in MiB",
        1024,
        lambda value: bounded_integer(value, 1, 1_000_000_000),
    )
    policy_file: Optional[str] = option(
        "Existing migration policy file (optional)", None, optional_path
    )
    quarantine_notification: str = option(
        "Default quarantine notification frequency when unavailable on source (never/hourly/daily/weekly)",
        "weekly",
        lambda value: choice_value(value, ("never", "hourly", "daily", "weekly")),
    )
    quarantine_category: str = option(
        "Default quarantine category when unavailable on source (add_header=Junk folder/reject=rejected/all=both)",
        "add_header",
        lambda value: choice_value(value, ("add_header", "reject", "all")),
    )

    def quarantine_attributes(self, source: Mapping[str, Any]) -> Mapping[str, str]:
        """Resolve normalized source mailbox attributes, using configured defaults for missing or null values."""
        return {
            item.name: item.metadata["validate"](
                getattr(self, item.name)
                if source.get(item.name) is None
                else source[item.name]
            )
            for item in fields(self)
            if item.name in ("quarantine_notification", "quarantine_category")
        }


SOURCE_SECTIONS = (
    ("postfixadmin", PostfixAdminConfiguration),
    ("plesk", PleskConfiguration),
)

SECTIONS = (
    *SOURCE_SECTIONS,
    ("mailcow", MailcowConfiguration),
    ("migration", MigrationConfiguration),
)


@dataclass(frozen=True)
class Configuration:
    mailcow: MailcowConfiguration
    migration: MigrationConfiguration
    postfixadmin: Optional[PostfixAdminConfiguration] = None
    plesk: Optional[PleskConfiguration] = None
    version: int = CONFIGURATION_VERSION

    def __post_init__(self) -> None:
        if not any(getattr(self, name) is not None for name, _ in SOURCE_SECTIONS):
            raise ConfigurationError(
                "Configure at least one source: PostfixAdmin or Plesk"
            )

    def document(self) -> Mapping[str, Any]:
        return {
            "version": self.version,
            **{
                name: asdict(section)
                for name, _ in SECTIONS
                if (section := getattr(self, name)) is not None
            },
        }


def section_label(name: str) -> str:
    return "PostfixAdmin" if name == "postfixadmin" else name.capitalize()


def section_from_document(schema: Type[Section], document: Any, name: str) -> Section:
    label = section_label(name)
    if not isinstance(document, dict):
        raise ConfigurationError(f"Missing or invalid configuration section: {label}")
    definitions = fields(schema)
    if set(document) - {item.name for item in definitions}:
        raise ConfigurationError(f"Unknown fields in configuration section: {label}")
    values = {}
    for item in definitions:
        try:
            values[item.name] = item.metadata["validate"](
                document.get(item.name, item.default)
            )
        except ConfigurationError as error:
            raise ConfigurationError(f"{label}.{item.name}: {error}") from error
    return schema(**values)


def configuration_from_document(document: Any) -> Configuration:
    if (
        not isinstance(document, dict)
        or type(document.get("version")) is not int
        or document["version"] != CONFIGURATION_VERSION
    ):
        raise ConfigurationError(
            f"Expected configuration version {CONFIGURATION_VERSION}"
        )
    if set(document) - {"version", *(name for name, _ in SECTIONS)}:
        raise ConfigurationError("Unknown top-level configuration fields")
    optional_sections = {name for name, _ in SOURCE_SECTIONS}
    return Configuration(
        **{
            name: None
            if name in optional_sections and document.get(name) is None
            else section_from_document(schema, document.get(name), name)
            for name, schema in SECTIONS
        }
    )


def load_configuration(path: Path) -> Configuration:
    try:
        return configuration_from_document(read_json(path, private=True))
    except (OSError, MigrationError) as error:
        detail = error.strerror if isinstance(error, OSError) else str(error)
        raise ConfigurationError(
            f"Cannot load configuration {path}: {detail}. Correct the file or generate a new one with mailcow-migrate configure --config FILE"
        ) from error
