import os
import sys
from pathlib import Path

from .accounts import (
    account_activation_mutations,
    account_journal,
    account_mutations,
    activation_started,
    validate_account_journal,
    verify_account_inventory,
)
from .configuration import Configuration
from .core.errors import ConfigurationError, MigrationError
from .core.state import Workspace
from .plans.recovery import recovery_plan
from .plans.saved import load_saved_plan
from .targets.account_inventory import account_inventory
from .targets.isolation import TargetIsolation, active_target_isolation
from .targets.mailcow import MailcowTarget
from .targets.webmail_state import WebmailJournal


def run_target_isolation(
    configuration: Configuration,
    file: Path,
    *,
    action: str = "preview",
    apply: bool = False,
    webmail_repair: bool = False,
) -> int:
    if sys.platform != "linux" or os.geteuid() != 0:
        raise ConfigurationError(
            "Run target isolation on the Linux Mailcow host as root"
        )
    if action not in ("preview", "pause", "check", "release") or apply != (
        action in ("pause", "release")
    ):
        raise ConfigurationError(
            "Use --apply with pause or release; preview and check are read-only"
        )
    with Workspace().lock():
        batch = load_saved_plan(file, configuration)
        state = account_journal(batch, configuration).read()
        if state.get("accounts") is not True or state.get("pending"):
            raise MigrationError(
                "Complete account staging and reconcile pending account requests before target isolation"
            )
        target = MailcowTarget(configuration.mailcow)
        current = active_target_isolation(target, batch, webmail_repair=webmail_repair)
        isolation = (
            current
            if action in ("check", "release")
            else TargetIsolation(target, batch, webmail_repair=webmail_repair)
        )
        if isolation.webmail_repair:
            if state.get("activated") is not True:
                raise MigrationError(
                    "Complete account activation before isolating a batch for webmail repair"
                )
            isolation.previous_isolation()
            if action in ("preview", "pause"):
                webmail = WebmailJournal(batch, configuration.mailcow)
                completed = webmail.read()
                if completed is not None and completed["complete"]:
                    webmail.require_complete()
                    raise MigrationError(
                        "Webmail import is already complete; use check/release for any remaining isolation"
                    )
            print(f"Using the separate webmail repair journal: {isolation.file}.")
        if action == "release":
            isolation.release(state)
            return 0
        if (
            not isolation.webmail_repair
            and action != "check"
            and (current.webmail_repair or activation_started(state))
        ):
            raise MigrationError(
                "Initial target isolation is forbidden after activation starts; use --webmail-repair after completed restoration"
            )
        recovery = recovery_plan(batch.plan)
        inventory = account_inventory(target.api)
        activation = (
            account_activation_mutations(batch.plan, inventory)
            if "activationRequests" in state
            else ()
        )
        staging = account_mutations(
            batch.plan,
            recovery["emails"],
            reuse_domain=bool(state.get("existingDomain")),
        )
        validate_account_journal(state, staging, activation)
        verify_account_inventory(batch.plan, inventory, recovery["emails"], state)
        if action == "preview":
            users, jobs = isolation.preflight()
            print(
                "Would defer delivery and block SOGo resource paths for: "
                + ", ".join(batch.plan["domains"])
            )
            print(
                f"Would pause protocol access for {len(users)} mailboxes, disconnect sessions and pause {len(jobs)} batch sync jobs."
            )
            print(
                f"Original access, jobs and configuration would be retained in {isolation.file}."
            )
            print(
                "Preview finished; no target changes. Remaining writers are checked after pausing. Source isolation is separate."
            )
        elif action == "pause":
            isolation.pause()
            print(
                "Target isolation established and checked. Keep its recovery journal; source isolation and message copying remain separate."
            )
        else:
            saved = isolation.state()
            if (
                not saved.get("paused")
                or saved.get("released")
                or saved.get("pending")
                or saved.get("routePending")
            ):
                raise MigrationError(
                    "No completed target pause to verify; resume the saved operation before proceeding"
                )
            isolation.check()
            print(
                "Target access, delivery holds, sessions and writers passed isolation checks. Source isolation has not been checked."
            )
    return 0
