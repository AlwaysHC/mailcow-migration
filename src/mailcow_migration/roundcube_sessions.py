import os
import sys
from pathlib import Path
from typing import Optional

from .accounts import account_journal, require_before_activation
from .configuration import Configuration
from .core.errors import ConfigurationError, MigrationError
from .core.state import Workspace
from .plans.saved import load_saved_plan
from .sources import SourceKind
from .sources.delivery import SourceDelivery
from .sources.logins import SourceLogins
from .sources.roundcube import roundcube_sessions


def run_roundcube_sessions(
    configuration: Configuration, file: Path, *, action: str = "preview", apply: bool = False,
    directory: Optional[str] = None, drafts_saved: bool = False,
) -> int:
    if sys.platform != "linux" or os.geteuid() != 0:
        raise ConfigurationError("Run RoundCube session operations on the Linux Mailcow host as root")
    if action not in ("preview", "revoke", "check") or apply != (action == "revoke") or action == "check" and drafts_saved:
        raise ConfigurationError("Use --apply only with revoke; preview/check leave source sessions unchanged")
    result = 0
    with Workspace().lock():
        batch = load_saved_plan(file, configuration)
        if action != "check":
            require_before_activation(account_journal(batch, configuration).read())
        sessions = roundcube_sessions(batch, configuration, directory)
        if action == "preview":
            record = sessions.preview(drafts_saved=drafts_saved)
            result = int(bool(record["blockers"]))
            print("Browser-only drafts saved on the server: {}.".format("confirmed" if record["drafts_saved"] else "not yet confirmed"))
            print("Preview saved; source user data and sessions were not changed.")
        elif action == "check":
            sessions.check()
            print("No batch RoundCube sessions remain; the selected user-data snapshot is unchanged.")
        else:
            if batch.source.kind is not SourceKind.POSTFIXADMIN:
                raise MigrationError("Plesk session revocation requires its source isolation adapter, which is not implemented yet")
            if batch.blocking_findings:
                raise MigrationError("Resolve saved planning findings before session revocation:\n" + batch.blocking_findings)
            if batch.plan["mailboxes"]:
                SourceLogins(batch).check()
            SourceDelivery(batch, configuration.mailcow.api).check()
            sessions.revoke(drafts_saved=drafts_saved)
            print("RoundCube session revocation completed and checked.")
        print("Webmail conversion/import and complete migration readiness remain pending; this operation does not activate accounts.")
    return result
