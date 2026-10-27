from typing import Optional, Sequence

from .errors import ConfigurationError


class WorkflowPaused(Exception):
    pass


def confirm_action(message: str, word: str = "apply") -> None:
    print(message)
    if input("Type {} to continue; Enter pauses: ".format(word)).strip() != word:
        raise WorkflowPaused("Paused. Existing settings, completed work and recovery journals are retained.")


def choose(message: str, choices: Sequence[str], default: Optional[str] = None) -> str:
    answer = input("{} ({}){}; q pauses: ".format(message, "/".join(choices), " [" + default + "]" if default else "")).strip()
    answer = answer or default or "q"
    if answer == "q":
        raise WorkflowPaused("Paused. Saved choices and recovery journals are retained.")
    if answer not in choices:
        raise ConfigurationError("Choose one of: " + ", ".join(choices))
    return answer
