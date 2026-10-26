import argparse
import logging
import sys
from pathlib import Path
from typing import Optional, Sequence

from . import __version__
from .accounts import run_accounts
from .admins import run_admins
from .activation import run_activation
from .configuration import load_configuration
from .core.errors import ConfigurationError, MigrationError
from .exporting import run_export
from .inspection import run_inspection
from .isolation import run_target_isolation
from .mail import run_mail
from .planning import run_plan
from .prefilters import run_prefilters
from .quota import run_quota
from .roundcube_sessions import run_roundcube_sessions
from .senders import run_senders
from .sogo_target import run_sogo_target
from .sieve import run_sieve
from .source_delivery import run_source_delivery
from .source_logins import run_source_logins
from .source_selection import run_source_selection
from .webmail import run_webmail
from .wizard import configure


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="mailcow-migrate", description="Prepare domain migration from PostfixAdmin and Plesk to Mailcow.",
        epilog="Preparation, account staging, target isolation and PostfixAdmin login and delivery controls are available. "
               "Transfers, activation and webmail import require stopped writers. Full isolation and final handoff remain pending.",
    )
    result.add_argument("--version", action="version", version="%(prog)s " + __version__)
    commands = result.add_subparsers(dest="command", required=True)
    for name, description in (
        ("configure", "Generate configuration interactively on the Linux Mailcow host"),
        ("check-config", "Validate a configuration file locally without contacting servers"),
        ("inspect", "Inspect source domain configuration, Maildirs and authoritative DNS without changing mail services"),
        ("select-source", "Select a source using DNS, saved batch identity and operation journals, including DNS handoff resume"),
        ("export", "Export private source configuration and account data without changing mail services"),
        ("plan", "Plan accounts and quotas from a completed export and inspect Mailcow without changing mail services"),
        ("accounts", "Preview or stage inactive accounts from a completed saved plan"),
        ("verify", "Verify account inventory against the saved plan and recorded activation phase"),
        ("activate", "Restore planned account activity under verified isolation after final mail, Sieve and filter checks"),
        ("quota", "Preview or recalculate quota for planned active mailboxes after activation, retaining isolation"),
        ("webmail", "Preview or import approved RoundCube data, editable filters and native settings while retaining isolation"),
        ("target-isolation", "Preview, pause or check batch access and writers on Mailcow; retain guarded restoration state"),
        ("source-logins", "Preview, freeze, check or release PostfixAdmin batch logins while preserving other query exclusions"),
        ("source-delivery", "Preview, pause or check PostfixAdmin delivery, recipient routing and RoundCube sessions"),
        ("roundcube-sessions", "Preview RoundCube user data and batch sessions; revoke or check sessions under verified source isolation"),
        ("sogo-target", "Preview or configure required SOGo settings and inspect existing user-data conflicts"),
        ("mail", "Preview or append source Maildirs under verified isolation; preserve existing target files"),
        ("sieve", "Preview or install exported personal/default Sieve with explicit replacement, backups and compilation"),
        ("prefilters", "Preview or stage inactive forwarding/vacation prefilters from the saved webmail plan"),
        ("senders", "Preview, merge or verify explicitly reviewed sender permissions while preserving existing grants"),
        ("admins", "Preview, import or verify reviewed domain administrators while preserving existing memberships and security"),
    ):
        command = commands.add_parser(name, help=description, description=description)
        command.add_argument("--config", type=Path, required=True, metavar="FILE",
                             help="JSON configuration file; create one with mailcow-migrate configure --config FILE")
        if name in ("inspect", "select-source", "export", "plan"):
            command.add_argument("domain", metavar="DOMAIN", help="Lowercase primary mail domain")
        if name == "inspect":
            command.add_argument("--output", type=Path, metavar="FILE", help="Save a private JSON report to a new file")
        if name == "select-source":
            command.add_argument("--plan", type=Path, metavar="FILE", help="Original saved plan, including older batches outside the discovery tree")
            command.add_argument("--record-target-addresses", action="store_true",
                                 help="After activation, save Mailcow addresses before changing DNS; requires --plan and root on the Mailcow host")
        if name in ("plan", "sieve", "prefilters", "activate", "webmail"):
            command.add_argument("--export", type=Path, required=True, metavar="DIR", help="Completed Python source export directory")
        if name == "plan":
            command.add_argument("--out", type=Path, required=True, metavar="NEW_DIR", help="New directory for private plan and assessment files")
            command.add_argument("--quota-mib", type=int, metavar="N", help="PostfixAdmin fixed-quota override; skips source sizing")
        if name in ("accounts", "verify", "target-isolation", "source-logins", "source-delivery",
                    "roundcube-sessions", "sogo-target", "mail", "sieve", "prefilters", "senders", "admins", "activate", "quota", "webmail"):
            command.add_argument("--plan", type=Path, required=True, metavar="FILE", help="Saved plan JSON from a completed planning run")
        if name == "accounts":
            command.add_argument("--apply", action="store_true", help="Apply the journaled account-staging operations to Mailcow")
        if name == "verify":
            command.add_argument("--passwords", action="store_true",
                                 help="Explicitly compare stored mailbox hashes with the plan; later password changes will report a mismatch")
        if name == "prefilters":
            command.add_argument("--apply", action="store_true", help="Stage reviewed prefilters inactive and verify their stored values")
        if name == "senders":
            action = command.add_mutually_exclusive_group()
            action.add_argument("--apply", action="store_true", help="Merge explicit grants into staged inactive mailboxes")
            action.add_argument("--check", action="store_true",
                                help="Read-only verification of completed sender permissions, including after activation")
        if name == "admins":
            action = command.add_mutually_exclusive_group()
            action.add_argument("--apply", action="store_true", help="Import domain administrators after account activation")
            action.add_argument("--check", action="store_true", help="Verify completed grants without changing memberships or passwords")
        if name in ("mail", "sieve", "activate", "quota", "webmail"):
            command.add_argument("--apply", action="store_true", help="Apply the selected operation and journal its result")
            command.add_argument("--writers-stopped", action="store_true",
                                 help="Confirm batch writers, submission and account administration are stopped on both servers")
        if name == "activate":
            command.add_argument("--ready", action="store_true", help="Confirm batch review and pre-activation cutover checks are complete")
        if name == "mail":
            command.add_argument("--final", action="store_true", help="Record a final mail pass only after an applied copy and live checks succeed")
        if name == "sieve":
            command.add_argument("--use-default-sieve", action="store_true",
                                 help="Use the exported default only for mailboxes without a personal script")
            command.add_argument("--replace-sieve", action="store_true",
                                 help="Back up and replace/remove conflicting target scripts and compiled files")
        if name == "target-isolation":
            command.add_argument("--action", choices=("preview", "pause", "check", "release"), default="preview",
                                 help="Defaults to read-only preview")
            command.add_argument("--apply", action="store_true",
                                 help="Required for pause/release; release requires completed activation and webmail import")
            command.add_argument("--webmail-repair", action="store_true",
                                 help="Use a separate pause/restore journal for incomplete webmail import after prior target restoration")
        if name == "source-logins":
            command.add_argument("--action", choices=("preview", "freeze", "check", "release"), default="preview",
                                 help="Defaults to read-only preview; check verifies a completed login freeze")
            command.add_argument("--apply", action="store_true", help="Required for freeze/release; both are forbidden after activation starts")
        if name == "source-delivery":
            command.add_argument("--action", choices=("preview", "pause", "check"), default="preview", help="Defaults to read-only preview")
            command.add_argument("--apply", action="store_true", help="Pause delivery and revoke RoundCube sessions after isolation and draft checks")
        if name == "roundcube-sessions":
            command.add_argument("--action", choices=("preview", "revoke", "check"), default="preview", help="Defaults to source read-only preview")
            command.add_argument("--directory", metavar="DIR", help="RoundCube installation on the source; pinned to this batch after preview")
            command.add_argument("--apply", action="store_true", help="Required for revoke; source login and delivery holds must verify")
            command.add_argument("--drafts-saved", action="store_true", help="Confirm all migrating users saved browser-only drafts on the server")
            command.add_argument("--reviewed", action="store_true", help="Confirm review of the previously saved, unchanged SOGo proposal")
            command.add_argument("--accept-deferrals", action="store_true", help="Explicitly defer the listed receipt/reply-folder preferences")
        if name == "sogo-target":
            command.add_argument("--action", choices=("preview", "configure", "check", "inspect-users", "prepare-users"), default="preview",
                                 help="Defaults to preview; prepare-users refreshes missing sources and cached unknown users before native checks")
            command.add_argument("--apply", action="store_true",
                                 help="Required for configure or prepare-users; a SOGo restart briefly interrupts webmail/DAV for all domains")
    return result


def main(arguments: Optional[Sequence[str]] = None) -> int:
    options = parser().parse_args(arguments)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        if options.command == "configure":
            configure(options.config)
            return 0
        configuration = load_configuration(options.config)
        if options.command == "inspect":
            return run_inspection(configuration, options.domain, options.output)
        elif options.command == "select-source":
            return run_source_selection(configuration, options.domain, plan=options.plan, record_target=options.record_target_addresses)
        elif options.command == "export":
            return run_export(configuration, options.domain)
        elif options.command == "plan":
            return run_plan(configuration, options.domain, options.export, options.out, options.quota_mib)
        elif options.command in ("accounts", "verify"):
            return run_accounts(configuration, options.plan,
                                apply=options.command == "accounts" and options.apply, verify=options.command == "verify",
                                passwords=options.command == "verify" and options.passwords)
        elif options.command == "activate":
            return run_activation(configuration, options.plan, options.export, apply=options.apply,
                                  ready=options.ready, writers_stopped=options.writers_stopped)
        elif options.command == "webmail":
            return run_webmail(configuration, options.plan, options.export, apply=options.apply,
                               writers_stopped=options.writers_stopped)
        elif options.command == "quota":
            return run_quota(configuration, options.plan, apply=options.apply, writers_stopped=options.writers_stopped)
        elif options.command == "target-isolation":
            return run_target_isolation(configuration, options.plan, action=options.action, apply=options.apply,
                                        webmail_repair=options.webmail_repair)
        elif options.command == "source-logins":
            return run_source_logins(configuration, options.plan, action=options.action, apply=options.apply)
        elif options.command == "source-delivery":
            return run_source_delivery(configuration, options.plan, action=options.action, apply=options.apply)
        elif options.command == "roundcube-sessions":
            return run_roundcube_sessions(configuration, options.plan, action=options.action, apply=options.apply,
                                          directory=options.directory, drafts_saved=options.drafts_saved,
                                          reviewed=options.reviewed, accept_deferrals=options.accept_deferrals)
        elif options.command == "sogo-target":
            return run_sogo_target(configuration, options.plan, action=options.action, apply=options.apply)
        elif options.command == "mail":
            return run_mail(configuration, options.plan, apply=options.apply,
                            final=options.final, writers_stopped=options.writers_stopped)
        elif options.command == "sieve":
            return run_sieve(configuration, options.plan, options.export, apply=options.apply,
                             use_default=options.use_default_sieve, replace=options.replace_sieve, writers_stopped=options.writers_stopped)
        elif options.command == "prefilters":
            return run_prefilters(configuration, options.plan, options.export, apply=options.apply)
        elif options.command == "senders":
            return run_senders(configuration, options.plan, apply=options.apply, check=options.check)
        elif options.command == "admins":
            return run_admins(configuration, options.plan, apply=options.apply, check=options.check)
        elif options.command == "check-config":
            print("Configuration is valid. Credential files and server connectivity have not been checked.")
        else:
            raise ConfigurationError("Unsupported command: " + options.command)
        return 0
    except MigrationError as error:
        logging.error("%s", error)
        return error.exit_code
    except OSError as error:
        logging.error("Filesystem or system operation failed: %s", error.strerror)
        return 1
    except EOFError:
        logging.error("Input ended; configuration was not saved")
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
