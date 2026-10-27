from ..configuration import Configuration
from ..plans.saved import SavedPlan
from . import SourceKind
from .delivery import SourceDelivery
from .plesk_isolation import PleskIsolation
from .submission import SourceSubmission


def check_source_holds(
    batch: SavedPlan, configuration: Configuration, *, webmail: bool = False
) -> None:
    if batch.source.kind is SourceKind.PLESK:
        PleskIsolation(batch, configuration).check(webmail=webmail)
    else:
        SourceSubmission(batch, configuration).check()
        SourceDelivery(batch, configuration.mailcow.api).check()
