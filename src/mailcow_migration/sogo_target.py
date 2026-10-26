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
    if action not in ("preview", "configure", "check", "inspect-users", "prepare-users") or apply != (action in ("configure", "prepare-users")):
        raise ConfigurationError("Use --apply with configure or prepare-users; other SOGo target actions are read-only")
    with Workspace().lock():
        batch = load_saved_plan(file, configuration)
        if apply and account_journal(batch, configuration).read().get("pending"):
            raise MigrationError("Reconcile the uncertain target account request before changing SOGo configuration or user lookups")
        prepared = sogo_preparation(configuration, batch, verify_source=action in ("inspect-users", "prepare-users"))
        if action == "preview":
            report = prepared.preview()
            for blocker in report.get("blockers", []):
                print("Required: " + blocker)
            return int(not report["settings_ready"])
        if action in ("inspect-users", "prepare-users"):
            return int(not prepared.inspect_users(prepare=action == "prepare-users"))
        if action == "configure":
            prepared.configure()
        config = prepared.preflight()
        print("Required SOGo tools, property-list conversion and effective identity settings passed checks.")
        if missing_user_sources(prepared.users, config):
            print("Domain user sources are not loaded yet; prepare-users --apply can refresh them with a shared SOGo restart.")
        print("Use inspect-users for native data/folder checks, then webmail for guarded import after activation.")
    return 0
