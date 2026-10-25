import os
import sys
from pathlib import Path

from .accounts import account_journal
from .configuration import Configuration
from .core.errors import ConfigurationError, MigrationError
from .core.state import Workspace
from .core.storage import local_path
from .plans.saved import SavedPlan, load_saved_plan
from .sources.roundcube import roundcube_sessions
from .targets.mailcow import MailcowTarget
from .targets.sogo import SogoTarget, missing_user_sources
from .targets.sogo_preparation import SogoPreparation


def sogo_preparation(configuration: Configuration, batch: SavedPlan, *, verify_source: bool = False) -> SogoPreparation:
    sessions = roundcube_sessions(batch, configuration)
    record = sessions.approved_record()
    if verify_source:
        sessions.check_source(record)
    proposal = sessions.proposal(record)
    directory = local_path(batch.directory / "sogo-target")
    return SogoPreparation(SogoTarget(MailcowTarget(configuration.mailcow), directory), batch, proposal)


def run_sogo_target(configuration: Configuration, file: Path, *, action: str = "preview", apply: bool = False) -> int:
    if sys.platform != "linux" or os.geteuid() != 0:
        raise ConfigurationError("Run SOGo target preparation on the Linux Mailcow host as root")
    if action not in ("preview", "configure", "check", "inspect-users") or apply != (action == "configure"):
        raise ConfigurationError("Use --apply only with configure; other SOGo target actions do not change configuration or user data")
    with Workspace().lock():
        batch = load_saved_plan(file, configuration)
        prepared = sogo_preparation(configuration, batch, verify_source=action == "inspect-users")
        if action == "preview":
            report = prepared.preview()
            for blocker in report.get("blockers", []):
                print("Required: " + blocker)
            return int(not report["settings_ready"])
        if action == "inspect-users":
            return int(not prepared.inspect_users())
        if action == "configure":
            if account_journal(batch, configuration).read().get("pending"):
                raise MigrationError("Reconcile the uncertain target account request before changing shared SOGo configuration")
            prepared.configure()
        config = prepared.preflight()
        print("Required SOGo tools, property-list conversion and effective identity settings passed checks.")
        if missing_user_sources(prepared.users, config):
            print("Domain user sources are not loaded yet; native user-data inspection and import remain pending.")
        print("This is target preparation only. User-data writes, target folder verification and full migration readiness remain pending.")
    return 0
