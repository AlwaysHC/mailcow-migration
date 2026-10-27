from dataclasses import asdict
from typing import Any, Mapping

from .admins import verify_admins
from .configuration import Configuration
from .core.errors import MigrationError
from .plans.saved import SavedPlan
from .plans.server_state import require_server_settings
from .senders import verify_senders
from .targets.isolation import active_target_isolation
from .targets.mailcow import MailcowTarget
from .targets.quota_state import QuotaJournal
from .targets.webmail_state import WebmailJournal
from .transfers import TransferContext


def handoff_identity(
    batch: SavedPlan, configuration: Configuration, kind: str
) -> Mapping[str, Any]:
    return {
        "version": 1,
        "kind": kind,
        "plan": batch.fingerprint,
        "source": asdict(batch.source),
        "api": configuration.mailcow.api,
        "mailcow_directory": configuration.mailcow.directory,
    }


def require_handoff_ready(
    batch: SavedPlan, configuration: Configuration
) -> MailcowTarget:
    target = MailcowTarget(configuration.mailcow)
    context = TransferContext(batch, configuration)
    context.final_state()
    inventory = context.check_accounts(target, activated=True)
    verify_senders(context, target, inventory)
    verify_admins(context, target)
    require_server_settings(batch, configuration)
    WebmailJournal(batch, configuration.mailcow).require_complete()
    QuotaJournal(batch, configuration.mailcow).require_complete(target)
    isolation = active_target_isolation(target, batch).state()
    if (
        isolation.get("released") is not True
        or isolation.get("pending")
        or isolation.get("routePending")
    ):
        raise MigrationError("Complete target access restoration before handoff")
    return target
