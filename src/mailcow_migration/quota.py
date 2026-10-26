from datetime import datetime, timezone
from pathlib import Path

from .configuration import Configuration
from .core.errors import MigrationError
from .core.state import Workspace
from .core.storage import json_fingerprint
from .plans.saved import load_saved_plan
from .sources import SourceKind
from .targets.mailcow import MailcowTarget
from .targets.quota_state import QuotaJournal, quota_binding, quota_volume
from .targets.reconciliation import api_number
from .transfers import TransferContext, require_transfer_host


def run_quota(configuration: Configuration, file: Path, *, apply: bool = False, writers_stopped: bool = False) -> int:
    require_transfer_host(writers_stopped)
    with Workspace().lock():
        batch = load_saved_plan(file, configuration)
        if batch.source.kind is not SourceKind.POSTFIXADMIN:
            raise MigrationError("Quota recalculation requires implemented activation/isolation; the Plesk adapter remains pending")
        if api_number(batch.plan["domainActive"]) != 1:
            raise MigrationError("Disabled primary domain: review quota recalculation under controlled maintenance")
        if batch.blocking_findings:
            raise MigrationError("Resolve saved planning findings before quota recalculation:\n" + batch.blocking_findings)
        context = TransferContext(batch, configuration)
        transfer = context.final_state()
        target = MailcowTarget(configuration.mailcow)
        journal = QuotaJournal(batch, configuration.mailcow)
        previous = journal.read(transfer)
        binding = quota_binding(transfer)
        context.target_holds(target)
        state = journal.begin(transfer) if apply else previous
        if apply:
            print("Private quota recalculation journal: {}.".format(journal.file))

        def verify_ready() -> None:
            if json_fingerprint(context.final_state(), canonical=True) != binding["transfer_sha256"]:
                raise MigrationError("Transfer receipt changed during quota recalculation; completion was not recorded")
            context.check_accounts(target, activated=True)
            context.check_holds(target)
            quota_volume(target, binding["containerRoot"])

        verify_ready()
        if not apply and previous is not None and not previous["complete"]:
            print("An earlier quota attempt is incomplete; --apply repeats recalculation under fresh checks.")
        for index, username in enumerate(journal.usernames, 1):
            if apply:
                verify_ready()
                state = journal.save({**state, "pending": username})
                print("Recalculating quota for {} ({}/{})...".format(username, index, len(journal.usernames)), flush=True)
                target.compose(("exec", "-T", "dovecot-mailcow", "doveadm", "quota", "recalc", "-u", username),
                               timeout=None, inherit_output=True)
                state = journal.save({**state, "completed": [*state["completed"], username], "pending": None})
            print("{}/{}: {} quota for {}.".format(index, len(journal.usernames), "Recalculated" if apply else "Would recalculate", username))
        verify_ready()
        if apply:
            journal.save({**state, "complete": True, "completed_at": datetime.now(timezone.utc).isoformat()})
        skipped = len(batch.plan["mailboxes"]) - len(journal.usernames)
        print("{} disabled mailboxes skipped; recalculate them under controlled maintenance before any future activation.".format(skipped))
        print("Quota recalculation completed; isolation remains in place." if apply else
              "Quota preview completed; no recalculation commands or receipt writes performed.")
    return 0
