from dataclasses import Field, fields
from pathlib import Path
from typing import Any, Callable, Dict, Tuple

from .configuration import (
    CONFIGURATION_VERSION,
    SECTIONS,
    SOURCE_SECTIONS,
    Configuration,
    configuration_from_document,
    load_configuration,
    section_label,
)
from .core.errors import ConfigurationError
from .core.prompts import WorkflowPaused
from .core.storage import local_path, read_json, write_private_json
from .transfers import require_mailcow_host


def ask_value(prompt: str, ask: Callable[[str], str]) -> str:
    entered = ask(prompt).strip()
    if entered == "q":
        raise WorkflowPaused("Configuration paused; saved answers are retained.")
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


def resume_configuration(
    path: Path, ask: Callable[[str], str], tell: Callable[[str], None]
) -> bool:
    tell(f"Saved configuration found: {path}")
    while True:
        choice = ask_value(
            "Continue saved setup or overwrite with new answers? [continue/overwrite/q]: ",
            ask,
        ).lower()
        if choice in ("continue", "overwrite"):
            return choice == "continue"
        tell("Enter continue, overwrite, or q to quit.")


def load_draft(path: Path) -> Dict[str, Any]:
    document = read_json(path, private=True)
    if (
        not isinstance(document, dict)
        or document.get("version") != CONFIGURATION_VERSION
    ):
        raise ConfigurationError(f"Invalid configuration draft version: {path}")
    if set(document) - {"version", *(name for name, _ in SECTIONS)}:
        raise ConfigurationError(f"Unknown configuration draft sections: {path}")
    if not any(name in document for name, _ in SOURCE_SECTIONS):
        raise ConfigurationError(f"Configuration draft has no selected source: {path}")
    for name, schema in SECTIONS:
        if name not in document:
            continue
        section = document[name]
        definitions = {item.name: item for item in fields(schema)}
        if not isinstance(section, dict) or set(section) - definitions.keys():
            raise ConfigurationError(f"Invalid configuration draft section: {name}")
        for key, value in section.items():
            try:
                definitions[key].metadata["validate"](value)
            except ConfigurationError as error:
                raise ConfigurationError(
                    f"{section_label(name)}.{key}: {error}"
                ) from error
    return document


def configure(
    path: Path, ask: Callable[[str], str] = input, tell: Callable[[str], None] = print
) -> Configuration:
    require_mailcow_host()
    destination = local_path(path)
    draft = destination.with_name(destination.name + ".draft.json")
    existing_configuration = destination.exists()
    existing_draft = draft.exists()
    document: Dict[str, Any] = {}
    if existing_draft or existing_configuration:
        if resume_configuration(draft if existing_draft else destination, ask, tell):
            if not existing_draft:
                return load_configuration(destination)
            document = load_draft(draft)
        else:
            tell(
                "Starting new answers. The final configuration is replaced only when all answers are complete."
            )
    tell(
        "Configure the source systems you use. Enter keeps a displayed default; q pauses and retains saved answers."
    )
    tell(
        "Enter credential file paths here, not passwords or API keys. Missing credentials are handled after saving."
    )
    if not document:
        selected_sources = ask_sources(ask, tell)
        document = {
            "version": CONFIGURATION_VERSION,
            **{name: {} for name in selected_sources},
        }
        write_private_json(draft, document, replace=existing_draft)
    tell(f"Answers are saved after each entry to {draft}.")
    skipped_sources = {name for name, _ in SOURCE_SECTIONS} - document.keys()
    for name, schema in SECTIONS:
        if name in skipped_sources:
            continue
        section = document.get(name, {})
        unanswered = tuple(item for item in fields(schema) if item.name not in section)
        if unanswered:
            tell(f"\n{section_label(name)} settings")
        for item in unanswered:
            value = ask_option(item, ask, tell)
            section = {**section, item.name: value}
            document = {**document, name: section}
            write_private_json(draft, document, replace=True)
    configuration = configuration_from_document(document)
    write_private_json(
        destination, configuration.document(), replace=existing_configuration
    )
    draft.unlink()
    tell(
        f"\nConfiguration saved to {destination}. Continuing with credentials and server readiness checks."
    )
    return configuration
