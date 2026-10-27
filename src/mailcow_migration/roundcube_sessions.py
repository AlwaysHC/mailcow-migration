import os
import sys
from pathlib import Path
from typing import Optional

from .accounts import account_journal, require_before_activation
from .configuration import Configuration
from .core.errors import ConfigurationError, MigrationError
from .core.state import Workspace
from .plans.saved import load_saved_plan
from .sources.isolation import check_source_holds
from .sources.roundcube import roundcube_sessions
from .targets.mailcow import MailcowTarget
from .targets.webmail_state import WebmailJournal
from .transfers import TransferContext


def run_roundcube_sessions(
    configuration: Configuration,
    file: Path,
    *,
    action: str = "preview",
    apply: bool = False,
    directory: Optional[str] = None,
    drafts_saved: bool = False,
    reviewed: bool = False,
    accept_deferrals: bool = False,
    webmail_repair: bool = False,
    writers_stopped: bool = False,
) -> int:
    if sys.platform != "linux" or os.geteuid() != 0:
        raise ConfigurationError(
            "Run RoundCube session operations on the Linux Mailcow host as root"
        )
    if (
        action not in ("preview", "revoke", "check")
        or apply != (action == "revoke")
        or action == "check"
        and drafts_saved
    ):
        raise ConfigurationError(
            "Use --apply only with revoke; preview/check leave source sessions unchanged"
        )
    if (
        (reviewed or accept_deferrals)
        and action != "preview"
        or accept_deferrals
        and not reviewed
    ):
        raise ConfigurationError(
            "Use --reviewed only with preview; --accept-deferrals also requires --reviewed"
        )
    if (
        webmail_repair
        and (action != "revoke" or not drafts_saved or not writers_stopped)
        or writers_stopped
        and not webmail_repair
    ):
        raise ConfigurationError(
            "Use --webmail-repair with revoke --apply --drafts-saved --writers-stopped"
        )
    result = 0
    with Workspace().lock():
        batch = load_saved_plan(file, configuration)
        if webmail_repair:
            target = MailcowTarget(configuration.mailcow)
            context = TransferContext(batch, configuration)
            context.check_accounts(target, activated=True)
            context.target_holds(target).check()
            completed = WebmailJournal(batch, configuration.mailcow).read()
            if completed is not None and completed["complete"]:
                raise MigrationError(
                    "Webmail import is complete; do not restart its session/import workflow"
                )
        elif action != "check":
            require_before_activation(account_journal(batch, configuration).read())
        sessions = roundcube_sessions(batch, configuration, directory)
        if action == "preview":
            record = sessions.preview(
                drafts_saved=drafts_saved,
                reviewed=reviewed,
                accept_deferrals=accept_deferrals,
            )
            result = int(bool(record["blockers"]))
            print(
                "Browser-only drafts saved on the server: {}.".format(
                    "confirmed" if record["drafts_saved"] else "not yet confirmed"
                )
            )
            print(
                "SOGo proposal review: {}.".format(
                    "recorded"
                    if record["reviewed"]
                    else "required before source isolation"
                )
            )
            print("Preview saved; source user data and sessions were not changed.")
        elif action == "check":
            sessions.check()
            print(
                "No batch RoundCube sessions remain; the selected user-data snapshot is unchanged."
            )
        else:
            if batch.blocking_findings:
                raise MigrationError(
                    "Resolve saved planning findings before session revocation:\n"
                    + batch.blocking_findings
                )
            check_source_holds(batch, configuration, webmail=webmail_repair)
            sessions.revoke(drafts_saved=drafts_saved)
            print("RoundCube session revocation completed and checked.")
        print(
            "Session controls are verified separately from webmail import and account activation."
        )
    return result
