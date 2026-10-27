import os
import sys
from pathlib import Path

from .accounts import require_source_recovery
from .configuration import Configuration
from .core.errors import ConfigurationError, MigrationError
from .core.state import Workspace
from .plans.saved import load_saved_plan
from .plans.server_state import require_server_settings
from .sogo_target import sogo_preparation
from .sources.delivery import SourceDelivery
from .sources.logins import SourceLogins, login_query
from .sources.roundcube import roundcube_sessions
from .sources.submission import SourceSubmission


def run_source_logins(
    configuration: Configuration,
    file: Path,
    *,
    action: str = "preview",
    apply: bool = False,
) -> int:
    if sys.platform != "linux" or os.geteuid() != 0:
        raise ConfigurationError(
            "Run source login isolation on the Linux Mailcow host as root"
        )
    if action not in ("preview", "freeze", "check", "release") or apply != (
        action in ("freeze", "release")
    ):
        raise ConfigurationError(
            "Use --apply with freeze or release; preview and check are read-only"
        )
    with Workspace().lock():
        batch = load_saved_plan(file, configuration)
        logins = SourceLogins(batch)
        if action != "check":
            require_source_recovery(batch, configuration)
        if not logins.users:
            print(
                "No mailbox logins in this batch; no source login operation is needed."
            )
            return 0
        if action == "check":
            logins.check()
            print(
                "Source login configuration is unchanged, authentication lookups are rejected and no batch Dovecot sessions remain."
            )
        else:
            findings = batch.blocking_findings
            if findings and action == "freeze":
                raise MigrationError(
                    "Resolve the saved planning findings before freezing source logins:\n"
                    + findings
                )
            if action == "freeze":
                require_server_settings(batch, configuration)
                SourceSubmission(batch, configuration).inspect()
                print(
                    "Checking source delivery prerequisites before changing logins..."
                )
                SourceDelivery(batch, configuration.mailcow.api).inspect()
                roundcube_sessions(batch, configuration).preflight()
                sogo_preparation(configuration, batch).preflight()
            snapshot = logins.inspect()
            if action == "preview":
                _, blocked = login_query(snapshot.query, logins.users, freeze=True)
                print(
                    "Source: {}; batch login block: {}.".format(
                        batch.source.ssh.host, "present" if blocked else "absent"
                    )
                )
                print("Would freeze only: " + ", ".join(logins.users))
                print(
                    "Freeze saves a source-side SQL configuration backup, excludes these logins, reloads Dovecot, "
                    "clears their authentication caches and disconnects their sessions."
                )
                print(
                    "Users must save drafts and close clients before freeze. Release removes only this batch's exclusion before activation."
                )
                if findings:
                    print(
                        "Resolve these saved planning findings before freeze:\n"
                        + findings
                    )
                print(
                    "Preview finished; no source configuration or login journal was changed."
                )
            else:
                logins.apply(snapshot, freeze=action == "freeze")
                print(
                    "Source login freeze completed."
                    if action == "freeze"
                    else "Source login exclusion removed. Migration stopped; review separate delivery holds before returning users."
                )
        print(
            "Source-delivery verifies SMTP readiness, delivery holds and RoundCube revocation after the login freeze."
        )
    return 0
