import argparse
import logging
import sys
from pathlib import Path
from typing import Optional, Sequence

from . import __version__
from .accounts import run_accounts
from .configuration import load_configuration
from .core.errors import MigrationError
from .exporting import run_export
from .inspection import run_inspection
from .planning import run_plan
from .wizard import configure


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="mailcow-migrate", description="Prepare domain migration from PostfixAdmin and Plesk to Mailcow.",
        epilog="Configuration, inspection, export, planning and inactive account staging are available. Mail transfer and activation remain pending.",
    )
    result.add_argument("--version", action="version", version="%(prog)s " + __version__)
    commands = result.add_subparsers(dest="command", required=True)
    for name, description in (
        ("configure", "Generate configuration interactively on the Linux Mailcow host"),
        ("check-config", "Validate a configuration file locally without contacting servers"),
        ("inspect", "Inspect source domain configuration, Maildirs and authoritative DNS without changing mail services"),
        ("export", "Export private source configuration and account data without changing mail services"),
        ("plan", "Plan accounts and quotas from a completed export and inspect Mailcow without changing mail services"),
        ("accounts", "Preview or stage inactive accounts from a completed saved plan"),
        ("verify", "Verify the account inventory of a saved staged batch without changing target accounts"),
    ):
        command = commands.add_parser(name, help=description, description=description)
        command.add_argument("--config", type=Path, required=True, metavar="FILE", help="JSON configuration file")
        if name in ("inspect", "export", "plan"):
            command.add_argument("domain", metavar="DOMAIN", help="Lowercase primary mail domain")
        if name == "inspect":
            command.add_argument("--output", type=Path, metavar="FILE", help="Save a private JSON report to a new file")
        if name == "plan":
            command.add_argument("--export", type=Path, required=True, metavar="DIR", help="Completed Python source export directory")
            command.add_argument("--out", type=Path, required=True, metavar="NEW_DIR", help="New directory for private plan and assessment files")
            command.add_argument("--quota-mib", type=int, metavar="N", help="PostfixAdmin fixed-quota override; skips source sizing")
        if name in ("accounts", "verify"):
            command.add_argument("--plan", type=Path, required=True, metavar="FILE", help="Saved plan JSON from a completed planning run")
        if name == "accounts":
            command.add_argument("--apply", action="store_true", help="Apply the journaled account-staging operations to Mailcow")
    return result


def main(arguments: Optional[Sequence[str]] = None) -> int:
    options = parser().parse_args(arguments)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        if options.command == "configure":
            configure(options.config)
        elif options.command == "inspect":
            return run_inspection(load_configuration(options.config), options.domain, options.output)
        elif options.command == "export":
            return run_export(load_configuration(options.config), options.domain)
        elif options.command == "plan":
            return run_plan(load_configuration(options.config), options.domain, options.export, options.out, options.quota_mib)
        elif options.command in ("accounts", "verify"):
            return run_accounts(load_configuration(options.config), options.plan,
                                apply=options.command == "accounts" and options.apply, verify=options.command == "verify")
        else:
            load_configuration(options.config)
            print("Configuration is valid. Credential files and server connectivity have not been checked.")
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
