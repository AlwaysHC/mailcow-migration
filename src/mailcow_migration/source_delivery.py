import os
import sys
from pathlib import Path

from .accounts import account_journal, require_before_activation
from .configuration import Configuration
from .core.errors import ConfigurationError, MigrationError
from .core.state import Workspace
from .plans.saved import load_saved_plan
from .plans.server_state import require_server_settings
from .sogo_target import sogo_preparation
from .sources.delivery import HOLD, SourceDelivery
from .sources.logins import SourceLogins
from .sources.roundcube import roundcube_sessions
from .sources.submission import SourceSubmission


def run_source_delivery(configuration: Configuration, file: Path, *, action: str = "preview", apply: bool = False) -> int:
    if sys.platform != "linux" or os.geteuid() != 0:
        raise ConfigurationError("Run source delivery isolation on the Linux Mailcow host as root")
    if action not in ("preview", "pause", "check") or apply != (action == "pause"):
        raise ConfigurationError("Use --action pause --apply to change delivery; preview and check are read-only")
    with Workspace().lock():
        batch = load_saved_plan(file, configuration)
        if action != "check":
            require_before_activation(account_journal(batch, configuration).read())
        delivery = SourceDelivery(batch, configuration.mailcow.api)
        sessions = roundcube_sessions(batch, configuration)
        if action == "check":
            if batch.plan["mailboxes"]:
                SourceLogins(batch).check()
            delivery.check()
            sessions.check()
            SourceSubmission(batch, configuration).check()
            print("Source delivery, recipient routing, queued envelopes, detected writers and RoundCube sessions passed checks.")
        else:
            findings = batch.blocking_findings
            if findings and action == "pause":
                raise MigrationError("Resolve saved planning findings before pausing source delivery:\n" + findings)
            snapshot = delivery.inspect()
            webmail_sessions = sessions.preflight(require_drafts=action == "pause")
            if action == "preview":
                request = delivery.routing_request
                held = sum(snapshot.transport["entries"].get(key) == HOLD for key in delivery.keys)
                print("Source: {}; {} of {} batch transport keys already held.".format(batch.source.ssh.host, held, len(delivery.keys)))
                print("Would preserve {} recipient addresses and catch-all behavior: {}.".format(len(request["recipients"]), request["catchAll"]))
                print("Would filter {} BCC maps, defer delivery and drain old Postfix workers.".format(len(snapshot.routing["bccMaps"])))
                print("Would revoke {} RoundCube sessions after delivery holds are installed.".format(sum(webmail_sessions.values())))
                print("Queued recipients are checked after recipient mappings are isolated; this preview does not establish queue safety.")
                print("Delivery holds remain until guarded handoff; automatic release is not available in this snapshot.")
                if findings:
                    print("Resolve these saved planning findings before pause:\n" + findings)
                print("Preview finished; no source configuration or delivery journal was changed.")
            else:
                require_server_settings(batch, configuration)
                submission = SourceSubmission(batch, configuration)
                submission_snapshot = submission.inspect()
                if batch.plan["mailboxes"]:
                    SourceLogins(batch).check()
                sogo_preparation(configuration, batch).preflight()
                delivery.pause(snapshot)
                sessions.revoke()
                submission.complete(submission_snapshot)
                print("Source delivery and SMTP isolation verified. Holds remain until source-forward is applied after target restoration.")
        print("Target isolation and later import/handoff stages remain separate requirements.")
    return 0
