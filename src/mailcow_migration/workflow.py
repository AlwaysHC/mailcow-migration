import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Optional
from uuid import uuid4

from .accounts import account_journal, activation_started, run_accounts
from .activation import run_activation
from .admins import run_admins
from .assessment import assess_batch
from .certificates import run_certificate
from .configuration import Configuration
from .core.domain import domain_name
from .core.errors import MigrationError
from .core.prompts import WorkflowPaused, choose, confirm_action
from .core.state import Workspace, require_identity
from .core.storage import json_fingerprint, local_path, read_json, write_private_json
from .exporting import export_domain
from .handoff import run_handoff
from .handoff_state import handoff_identity
from .isolation import run_target_isolation
from .mail import run_mail
from .planning import run_plan
from .plans.saved import SavedPlan, load_saved_plan
from .prefilters import run_prefilters
from .quota import run_quota
from .roundcube_sessions import run_roundcube_sessions
from .senders import run_senders
from .sieve import run_sieve
from .signing import run_signing
from .sogo_target import run_sogo_target
from .source_delivery import run_source_delivery
from .source_forwarding import run_source_forwarding
from .source_isolation import run_source_isolation
from .source_logins import run_source_logins
from .source_selection import record_target_addresses, select_domain_source
from .sources import SourceKind
from .sources.roundcube import roundcube_sessions
from .targets.isolation import active_target_isolation
from .targets.mailcow import MailcowTarget
from .targets.quota_state import QuotaJournal
from .targets.webmail_state import WebmailJournal
from .transfers import require_mailcow_host
from .webmail import run_webmail


def prompt_value(message: str) -> str:
    value = input(message + "; Enter pauses: ").strip()
    if not value or value == "q":
        raise WorkflowPaused(
            "Paused. Saved choices and recovery journals are retained."
        )
    return value


@dataclass(frozen=True)
class GuidedMigration:
    configuration: Configuration
    batch: SavedPlan
    exported: Path

    @property
    def file(self) -> Path:
        return self.batch.directory / "plan.json"

    @property
    def target(self) -> MailcowTarget:
        return MailcowTarget(self.configuration.mailcow)

    def accounts(self) -> Mapping[str, Any]:
        return account_journal(self.batch, self.configuration).read()

    def workflow(self) -> Mapping[str, Any]:
        identity = {
            "version": 1,
            "kind": "guided-domain-migration",
            "plan": self.batch.fingerprint,
            "source": asdict(self.batch.source),
            "configuration": self.configuration.document(),
            "export": str(self.exported),
            "policy_sha256": json_fingerprint(
                read_json(self.batch.directory / "policy.json", private=True),
                canonical=True,
            ),
        }
        file = local_path(self.batch.directory / "workflow.json")
        if file.exists():
            state = read_json(file, private=True)
            require_identity(state, identity)
            if any(
                type(state.get(key)) is not bool
                for key in ("use_default_sieve", "replace_sieve")
            ):
                raise MigrationError(
                    "Invalid saved Sieve choices; retain the guided workflow journal"
                )
            return state
        use_default = (
            choose(
                "Use exported default Sieve for users without a personal script",
                ("no", "yes"),
                "no",
            )
            == "yes"
        )
        replace = (
            choose(
                "Back up and replace conflicting target Sieve scripts",
                ("no", "yes"),
                "no",
            )
            == "yes"
        )
        state = {**identity, "use_default_sieve": use_default, "replace_sieve": replace}
        confirm_action(
            "Pin this batch, its reviewed policy and Sieve choices before beginning target or source changes.",
            "begin",
        )
        write_private_json(file, state)
        return state

    def require_assessment(
        self, phase: str, *, hostname: Optional[str] = None
    ) -> Mapping[str, Any]:
        report = assess_batch(
            self.configuration,
            self.file,
            exported=self.exported,
            phase=phase,
            hostname=hostname,
        )
        if not report["ready_for_phase"]:
            raise MigrationError(
                "Assessment is blocked. Resolve its reported findings before continuing this saved batch."
            )
        return report

    def source_recovery(self) -> None:
        name = (
            "source-isolation.json"
            if self.batch.source.kind is SourceKind.PLESK
            else "source-logins.json"
        )
        if not (self.batch.directory / name).exists():
            return
        plesk = self.batch.source.kind is SourceKind.PLESK
        print(
            "Source isolation has a saved journal. Release stops migration and retains target data and recovery evidence."
        )
        print(
            "Release restores Plesk access/delivery."
            if plesk
            else "Release removes only the login exclusion; delivery holds remain."
        )
        if (
            choose(
                "Resume migration or release the source controls",
                ("continue", "release"),
            )
            == "release"
        ):
            operation = run_source_isolation if plesk else run_source_logins
            operation(self.configuration, self.file, action="release", apply=True)
            raise WorkflowPaused(
                "Source recovery completed. Migration stopped; review delivery and client routing before returning users."
            )

    def review_roundcube(self) -> None:
        if (
            self.batch.source.roundcube_directory is None
            and not (self.batch.directory / "roundcube-sessions/state.json").exists()
        ):
            directory = prompt_value(
                "Absolute RoundCube installation directory on the source"
            )
            if run_roundcube_sessions(
                self.configuration, self.file, directory=directory
            ):
                raise MigrationError(
                    "Resolve the RoundCube proposal blockers before isolating the source"
                )
        sessions = roundcube_sessions(self.batch, self.configuration)
        record = sessions.record()
        if record is not None and record["attempts"]:
            sessions.preflight(require_drafts=True)
            return
        if run_roundcube_sessions(self.configuration, self.file):
            raise MigrationError(
                "Resolve the RoundCube proposal blockers before isolating the source"
            )
        confirm_action(
            "Review the saved RoundCube proposal, identities, contacts and folder mappings.",
            "reviewed",
        )
        record = sessions.record()
        deferred = sessions.proposal(record, require_review=False)[
            "deferredPreferences"
        ]
        if deferred:
            confirm_action(
                "Explicitly defer the listed unsupported receipt/reply-folder preferences.",
                "defer",
            )
        confirm_action(
            "All migrating users have saved browser-only drafts to the server and closed their clients.",
            "saved",
        )
        if run_roundcube_sessions(
            self.configuration,
            self.file,
            reviewed=True,
            accept_deferrals=bool(deferred),
            drafts_saved=True,
        ):
            raise MigrationError(
                "The reviewed RoundCube source changed or remains blocked"
            )

    def stage_and_copy(self) -> None:
        self.source_recovery()
        self.review_roundcube()
        report = assess_batch(self.configuration, self.file, exported=self.exported)
        if any(row["status"] == "blocked" for row in report["checks"]):
            raise MigrationError(
                "Resolve the complete assessment before any guided target or source changes"
            )
        choices = self.workflow()
        if any(
            row["id"] == "sogo-configuration" and row["status"] == "required"
            for row in report["checks"]
        ):
            confirm_action(
                "Enable the reviewed SOGo custom identities. "
                "A shared restart briefly interrupts webmail/DAV for all domains.",
                "configure",
            )
            run_sogo_target(
                self.configuration, self.file, action="configure", apply=True
            )
            self.require_assessment("before")
        confirm_action(
            "Confirm capacity/inodes, quotas and backups. Custom writers, submission and account administration are stopped.",
            "stopped",
        )
        if self.batch.source.kind is SourceKind.PLESK:
            run_source_isolation(
                self.configuration,
                self.file,
                action="freeze",
                apply=True,
                writers_stopped=True,
            )
        run_accounts(self.configuration, self.file, apply=True)
        if self.batch.source.kind is SourceKind.POSTFIXADMIN:
            run_source_logins(
                self.configuration, self.file, action="freeze", apply=True
            )
            run_source_delivery(
                self.configuration, self.file, action="pause", apply=True
            )
        run_target_isolation(self.configuration, self.file, action="pause", apply=True)
        self.require_assessment("staged")
        confirm_action(
            "Append and verify all batch mail under isolation, then install the reviewed Sieve choices.",
            "copy",
        )
        run_mail(
            self.configuration, self.file, apply=True, final=True, writers_stopped=True
        )
        run_sieve(
            self.configuration,
            self.file,
            self.exported,
            apply=True,
            writers_stopped=True,
            use_default=choices["use_default_sieve"],
            replace=choices["replace_sieve"],
        )
        run_prefilters(self.configuration, self.file, self.exported, apply=True)
        run_senders(self.configuration, self.file, apply=True)

    def import_webmail(self) -> None:
        journal = WebmailJournal(self.batch, self.configuration.mailcow)
        state = journal.read()
        if state is not None and state["complete"]:
            journal.require_complete()
            print("Completed webmail import retained; later user edits are preserved.")
            return
        isolation = active_target_isolation(self.target, self.batch)
        isolation_state = isolation.state()
        if isolation_state.get("pending") == "release":
            raise MigrationError(
                "Target restoration already started with webmail incomplete; reconcile the isolation journal before import"
            )
        repair = isolation.webmail_repair or isolation_state.get("released") is True
        confirm_action(
            "Confirm custom source/target writers remain stopped and all browser drafts are saved on the server.",
            "stopped",
        )
        if repair:
            confirm_action(
                "Pause only this batch for unfinished webmail repair; account activation and copied mail will be retained.",
                "pause",
            )
            run_target_isolation(
                self.configuration,
                self.file,
                action="pause",
                apply=True,
                webmail_repair=True,
            )
            run_roundcube_sessions(
                self.configuration,
                self.file,
                action="revoke",
                apply=True,
                drafts_saved=True,
                writers_stopped=True,
                webmail_repair=True,
            )
        quota = QuotaJournal(self.batch, self.configuration.mailcow)
        previous_quota = quota.read()
        if previous_quota is None or not previous_quota["complete"]:
            run_quota(self.configuration, self.file, apply=True, writers_stopped=True)
        else:
            quota.require_complete(self.target)
        run_admins(self.configuration, self.file, apply=True)
        native_file = self.batch.directory / "sogo-target/native-import-state.json"
        if not native_file.exists():
            confirm_action(
                "Prepare SOGo user lookups and inspect destinations. Missing sources may require a shared SOGo restart.",
                "prepare",
            )
            if run_sogo_target(
                self.configuration, self.file, action="prepare-users", apply=True
            ):
                raise MigrationError(
                    "Resolve the native user-data conflicts before importing webmail"
                )
        self.require_assessment("activated")
        confirm_action(
            "Import the reviewed webmail data under isolation, retaining backups and all completed stage receipts.",
            "import",
        )
        run_webmail(
            self.configuration,
            self.file,
            self.exported,
            apply=True,
            writers_stopped=True,
        )

    def client_hostname(self) -> str:
        names = set()
        for name, kind in (
            ("completed.json", "mail-migration-complete"),
            ("dns-before.json", "mail-handoff"),
            ("mail-certificate-request.json", "mail-certificate"),
            ("mail-certificate.json", "mail-certificate"),
        ):
            file = local_path(self.batch.directory / name)
            if file.exists():
                saved = read_json(file, private=True)
                require_identity(
                    saved, handoff_identity(self.batch, self.configuration, kind)
                )
                value = saved.get("hostname")
                if not isinstance(value, str):
                    raise MigrationError(
                        "Missing saved client hostname; retain its handoff evidence"
                    )
                names.add(domain_name(value))
        if len(names) > 1:
            raise MigrationError(
                "Certificate and handoff hostname choices differ; reconcile the saved evidence"
            )
        if names:
            return next(iter(names))
        default = "mail." + self.batch.plan["domain"]
        return (
            default
            if choose("Client hostname: " + default, ("default", "custom"), "default")
            == "default"
            else domain_name(
                prompt_value("Client hostname"),
            )
        )

    def handoff(self) -> None:
        isolation = active_target_isolation(self.target, self.batch)
        state = isolation.state()
        if (
            state.get("released") is not True
            or state.get("pending")
            or state.get("routePending")
        ):
            confirm_action(
                "Restore batch access and delivery on Mailcow after verifying all import receipts.",
                "release",
            )
            run_target_isolation(
                self.configuration, self.file, action="release", apply=True
            )
        record_target_addresses(self.batch, self.configuration)
        name = self.client_hostname()
        print(
            f"Point {name} A/AAAA directly to Mailcow before certificate setup; retain source MX until forwarding is ready."
        )
        run_certificate(self.configuration, self.file, hostname=name)
        confirm_action(
            "Verify/request the mail hostname certificate. "
            "An uncertain earlier refresh requires explicit certificate --action retry.",
            "certificate",
        )
        run_certificate(
            self.configuration, self.file, hostname=name, action="apply", apply=True
        )
        signing = self.batch.directory / "signing-choice.json"
        options = {}
        if not signing.exists():
            options["signer"] = choose("DKIM signer", ("mailcow", "relay"), "mailcow")
            if options["signer"] == "relay":
                options["relay_id"] = prompt_value("Existing active Mailcow relay ID")
                options["dkim_name"] = prompt_value(
                    "Provider selector._domainkey.DOMAIN"
                )
                key = choose("Provider DNS record type", ("cname", "public-key"))
                options["cname" if key == "cname" else "public_key"] = prompt_value(
                    "Provider CNAME target"
                    if key == "cname"
                    else "Provider public key",
                )
                if key == "public-key":
                    options["key_type"] = choose(
                        "Provider public key algorithm", ("rsa", "ed25519"), "rsa"
                    )
        run_signing(self.configuration, self.file, **options)
        confirm_action(
            "Save this signing choice; generate only a missing Mailcow key or assign the reviewed existing relay.",
            "signing",
        )
        run_signing(self.configuration, self.file, apply=True, **options)
        forwarding = self.batch.directory / "source-forwarding.json"
        address = (
            read_json(forwarding, private=True)["ip"]
            if forwarding.exists()
            else prompt_value(
                "Fixed Mailcow IP reachable from the source on TCP 25",
            )
        )
        run_source_forwarding(self.configuration, self.file, address)
        confirm_action(
            "Start and verify real source-to-Mailcow forwarding for this batch and its queued recipients.",
            "forward",
        )
        run_source_forwarding(self.configuration, self.file, address, apply=True)
        run_handoff(self.configuration, self.file, hostname=name)
        confirm_action(
            "Publish/review the displayed DNS changes and allow the recorded TTLs to expire before live checks.",
            "check",
        )
        self.require_assessment("handoff", hostname=name)
        confirm_action(
            "Real inbound/outbound delivery, aliases and catch-all behavior passed.",
            "delivery",
        )
        confirm_action(
            "Client logins, folders, message contents, attachments and webmail data passed.",
            "clients",
        )
        confirm_action(
            "Received SPF/DKIM/DMARC and any relay Return-Path results passed.",
            "authentication",
        )
        confirm_action(
            "Allowed and rejected send-as attempts and scoped BCC delivery passed.",
            "permissions",
        )
        run_handoff(
            self.configuration,
            self.file,
            hostname=name,
            action="complete",
            apply=True,
            delivery_confirmed=True,
            clients_confirmed=True,
            message_auth_confirmed=True,
            permissions_confirmed=True,
        )

    def run(self) -> int:
        workflow = self.batch.directory / "workflow.json"
        if workflow.exists():
            self.workflow()
        if (self.batch.directory / "completed.json").exists():
            return run_handoff(
                self.configuration,
                self.file,
                hostname=self.client_hostname(),
                action="check",
            )
        state = self.accounts()
        if not activation_started(state):
            self.stage_and_copy()
        if self.accounts().get("activated") is not True:
            confirm_action(
                "Confirm cutover readiness and stopped writers; "
                "activate the reviewed accounts under retained source/target isolation.",
                "activate",
            )
            run_activation(
                self.configuration,
                self.file,
                self.exported,
                apply=True,
                ready=True,
                writers_stopped=True,
            )
        self.import_webmail()
        self.handoff()
        return 0


def run_migration(
    configuration: Configuration,
    domain: str,
    *,
    plan: Optional[Path] = None,
    exported: Optional[Path] = None,
    fixed_quota: Optional[int] = None,
) -> int:
    require_mailcow_host()
    domain_name(domain)
    if not sys.stdin.isatty():
        raise MigrationError(
            "Guided migration requires an interactive terminal; use explicit component commands for individual operations"
        )
    workspace = Workspace()
    with workspace.lock():
        pointer = workspace.path("guided/" + domain + ".json")
        identity = {
            "version": 1,
            "kind": "guided-plan-selection",
            "domain": domain,
            "configuration": configuration.document(),
        }
        if plan is None and pointer.exists():
            selected = read_json(pointer, private=True)
            require_identity(selected, identity)
            if not isinstance(selected.get("plan"), str):
                raise MigrationError(
                    "Invalid guided plan selection; retain the saved file"
                )
            plan = Path(selected["plan"])
        _, batch = select_domain_source(configuration, domain, workspace, plan)
        if batch is None:
            if exported is None:
                confirm_action(
                    "Export this domain privately for planning; mail services will remain unchanged.",
                    "export",
                )
                exported = export_domain(configuration, domain)
            destination = workspace.path(f"plans/{domain}/{uuid4().hex}")
            if run_plan(configuration, domain, exported, destination, fixed_quota):
                raise MigrationError(
                    "Planning found blockers or decisions. Review the retained plan before creating a corrected batch."
                )
            batch = load_saved_plan(destination / "plan.json", configuration)
        elif fixed_quota is not None:
            raise MigrationError(
                "Quota choices are already pinned in this saved plan; --quota-mib applies only to new plans"
            )
        manifest = read_json(batch.directory / "manifest.json", private=True)
        original_export = local_path(Path(manifest["export"]))
        if exported is not None and local_path(exported) != original_export:
            raise MigrationError(
                "The guided workflow must use the original source export from its saved plan"
            )
        write_private_json(
            pointer,
            {**identity, "plan": str(batch.directory / "plan.json")},
            replace=True,
        )
        print(
            f"Guided batch: {batch.directory}. Review its private summary, policy and assessment; q or an empty confirmation pauses."
        )
        return GuidedMigration(configuration, batch, original_export).run()
