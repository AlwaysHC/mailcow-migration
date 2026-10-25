import os
import sys
from pathlib import Path

from .accounts import account_journal, account_mutations, validate_account_journal, verify_account_inventory
from .configuration import Configuration
from .core.errors import ConfigurationError, MigrationError
from .core.state import Workspace
from .plans.recovery import recovery_plan
from .plans.saved import load_saved_plan
from .targets.account_inventory import account_inventory
from .targets.isolation import TargetIsolation
from .targets.mailcow import MailcowTarget


def run_target_isolation(configuration: Configuration, file: Path, *, action: str = "preview", apply: bool = False) -> int:
    if sys.platform != "linux" or os.geteuid() != 0:
        raise ConfigurationError("Run target isolation on the Linux Mailcow host as root")
    if action not in ("preview", "pause", "check", "release") or apply != (action in ("pause", "release")):
        raise ConfigurationError("Use --apply with pause or release; preview and check are read-only")
    with Workspace().lock():
        batch = load_saved_plan(file, configuration)
        state = account_journal(batch, configuration).read()
        if state.get("accounts") is not True or state.get("pending"):
            raise MigrationError("Complete account staging and reconcile pending account requests before target isolation")
        target = MailcowTarget(configuration.mailcow)
        isolation = TargetIsolation(target, batch)
        if action == "release":
            isolation.release(state)
            return 0
        if state.get("activated") or "enable domain for activation" in state["done"]:
            raise MigrationError("Initial target isolation is forbidden after activation starts; use the matching recovery workflow")
        recovery = recovery_plan(batch.plan)
        validate_account_journal(state, account_mutations(batch.plan, recovery["emails"], reuse_domain=bool(state.get("existingDomain"))))
        verify_account_inventory(batch.plan, account_inventory(target.api), recovery["emails"], state)
        if action == "preview":
            users, jobs = isolation.preflight()
            print("Would defer delivery and block SOGo resource paths for: " + ", ".join(batch.plan["domains"]))
            print("Would pause protocol access for {} mailboxes, disconnect sessions and pause {} batch sync jobs.".format(len(users), len(jobs)))
            print("Original access, jobs and configuration would be retained in {}.".format(isolation.file))
            print("Preview finished; no target changes. Remaining writers are checked after pausing. Source isolation is separate.")
        elif action == "pause":
            isolation.pause()
            print("Target isolation established and checked. Keep its recovery journal; source isolation and message copying remain separate.")
        else:
            saved = isolation.state()
            if not saved.get("paused") or saved.get("released") or saved.get("pending") or saved.get("routePending"):
                raise MigrationError("No completed target pause to verify; resume the saved operation before proceeding")
            isolation.check()
            print("Target access, delivery holds, sessions and writers passed isolation checks. Source isolation has not been checked.")
    return 0
