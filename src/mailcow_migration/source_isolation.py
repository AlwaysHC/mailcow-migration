from pathlib import Path

from .accounts import require_source_recovery
from .configuration import Configuration
from .core.errors import ConfigurationError, MigrationError
from .core.state import Workspace
from .plans.saved import load_saved_plan
from .sogo_target import sogo_preparation
from .sources import SourceKind
from .sources.plesk_isolation import PleskIsolation
from .sources.roundcube import roundcube_sessions
from .transfers import require_mailcow_host


def run_source_isolation(
    configuration: Configuration, file: Path, *, action: str = "preview", apply: bool = False, writers_stopped: bool = False,
) -> int:
    require_mailcow_host()
    if action not in ("preview", "freeze", "check", "release") or apply != (action in ("freeze", "release")):
        raise ConfigurationError("Use --apply with freeze/release; preview/check leave source settings unchanged")
    with Workspace().lock():
        batch = load_saved_plan(file, configuration)
        if batch.source.kind is not SourceKind.PLESK:
            raise MigrationError("Use source-logins and source-delivery for the current PostfixAdmin isolation path")
        if action != "check":
            require_source_recovery(batch, configuration)
        source = PleskIsolation(batch, configuration)
        if action == "release":
            source.change("release")
            print("Source access and delivery restored before activation. Target staging/isolation and recovery evidence remain.")
            return 0
        sessions = roundcube_sessions(batch, configuration)
        if action == "check":
            source.check()
            sessions.check()
            return 0
        environment = source.request("inspect")
        source.check_source()
        counts = sessions.preflight(require_drafts=action == "freeze")
        print("Plesk Dovecot {}; Postfix {}; SMTP authentication {}.".format(
            environment["dovecot"], environment["postfix"], environment["sasl"],
        ))
        print("Batch {}: {} sessions; source recovery state: {}.".format(batch.plan["domain"], sum(counts.values()), environment["stateFile"]))
        if action == "preview":
            print("Would block logins/submission/delivery, disconnect users, drain workers and revoke reviewed RoundCube sessions.")
            return 0
        if not writers_stopped:
            raise ConfigurationError("Use --writers-stopped after pausing custom imports and source account administration")
        if batch.blocking_findings:
            raise MigrationError("Resolve saved planning findings before source isolation:\n" + batch.blocking_findings)
        sogo_preparation(configuration, batch).preflight()
        source.change("freeze")
        sessions.revoke()
        source.check()
        sessions.check()
        print("Plesk isolation and RoundCube revocation verified. Retain all journals until guarded handoff completes.")
    return 0
