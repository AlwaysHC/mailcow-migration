import sys
from dataclasses import Field, fields
from pathlib import Path
from typing import Any, Callable, Dict, Tuple

from .configuration import (
    CONFIGURATION_VERSION,
    SECTIONS,
    SOURCE_SECTIONS,
    Configuration,
    configuration_from_document,
    section_label,
)
from .core.errors import ConfigurationError
from .core.storage import local_path, write_private_json


def ask_value(prompt: str, ask: Callable[[str], str]) -> str:
    entered = ask(prompt).strip()
    if entered == "q":
        raise ConfigurationError("Configuration cancelled; no file was written")
    return entered


def ask_sources(
    ask: Callable[[str], str], tell: Callable[[str], None]
) -> Tuple[str, ...]:
    choices: Dict[str, Tuple[str, ...]] = {
        str(index): (name,) for index, (name, _) in enumerate(SOURCE_SECTIONS, start=1)
    }
    both = str(len(choices) + 1)
    tell("\nSelect source systems:")
    for key, (name,) in choices.items():
        tell(f"  {key}. {section_label(name)}")
    tell(f"  {both}. Both")
    choices[both] = tuple(name for name, _ in SOURCE_SECTIONS)
    while True:
        selected = ask_value("Source selection: ", ask)
        if selected in choices:
            return choices[selected]
        tell(f"Choose one of: {', '.join(choices)}")


def ask_option(
    item: Field, ask: Callable[[str], str], tell: Callable[[str], None]
) -> Any:
    default = item.default
    hint = f" [{default}]" if default is not None and default != "" else ""
    while True:
        entered = ask_value("{}{}: ".format(item.metadata["label"], hint), ask)
        try:
            value = (
                default
                if not entered
                else int(entered)
                if type(default) is int
                else entered
            )
            return item.metadata["validate"](value)
        except (ConfigurationError, ValueError) as error:
            tell(
                str(error)
                if isinstance(error, ConfigurationError)
                else "Enter a whole number"
            )


def configure(
    path: Path, ask: Callable[[str], str] = input, tell: Callable[[str], None] = print
) -> Configuration:
    if sys.platform != "linux":
        raise ConfigurationError(
            "Run the configuration wizard on the Linux Mailcow host"
        )
    destination = local_path(path)
    if destination.exists():
        raise ConfigurationError(
            f"Configuration already exists; edit it or choose a new --config path: {destination}"
        )
    tell(
        "Configure the source systems you use. Enter keeps a displayed default; q cancels."
    )
    tell(
        "Credential paths refer to existing files; do not enter passwords or API keys here."
    )
    selected_sources = ask_sources(ask, tell)
    skipped_sources = {name for name, _ in SOURCE_SECTIONS} - set(selected_sources)
    document = {"version": CONFIGURATION_VERSION}
    for name, schema in SECTIONS:
        if name in skipped_sources:
            continue
        tell(f"\n{section_label(name)} settings")
        document[name] = {
            item.name: ask_option(item, ask, tell) for item in fields(schema)
        }
    configuration = configuration_from_document(document)
    write_private_json(destination, configuration.document())
    tell(
        f"\nConfiguration saved to {destination}. Credential files and server connectivity have not been checked."
    )
    return configuration
