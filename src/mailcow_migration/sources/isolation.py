from . import SourceKind
from .delivery import SourceDelivery
from .logins import SourceLogins
from .plesk_isolation import PleskIsolation
from ..configuration import Configuration
from ..plans.saved import SavedPlan


def check_source_holds(batch: SavedPlan, configuration: Configuration, *, webmail: bool = False) -> None:
    if batch.source.kind is SourceKind.PLESK:
        PleskIsolation(batch, configuration).check(webmail=webmail)
    else:
        if batch.plan["mailboxes"]:
            SourceLogins(batch).check()
        SourceDelivery(batch, configuration.mailcow.api).check()
