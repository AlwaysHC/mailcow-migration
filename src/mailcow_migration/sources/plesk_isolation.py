from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Optional
from uuid import uuid4

from ..configuration import Configuration
from ..core.errors import MigrationError
from ..core.state import require_identity
from ..core.storage import json_fingerprint, local_path, read_json, write_private_json
from ..plans.evidence import load_plan_export
from ..plans.saved import SavedPlan
from .plesk import plesk_request, read_plesk


@dataclass(frozen=True)
class PleskIsolation:
    batch: SavedPlan
    configuration: Configuration

    @property
    def file(self) -> Path:
        return local_path(self.batch.directory / "source-isolation.json")

    @property
    def identity(self) -> Mapping[str, Any]:
        return {
            "version": 1,
            "kind": "plesk-source-isolation",
            "source": asdict(self.batch.source),
            "plan": self.batch.fingerprint,
            "api": self.configuration.mailcow.api,
            "mailcow_directory": self.configuration.mailcow.directory,
        }

    def state(self) -> Optional[Mapping[str, Any]]:
        if not self.file.exists():
            return None
        state = read_json(self.file, private=True)
        if (
            not isinstance(state, dict)
            or not isinstance(state.get("identity"), dict)
            or state.get("action") not in ("freeze", "release")
            or type(state.get("complete")) is not bool
        ):
            raise MigrationError(
                "Invalid Plesk isolation journal; retain " + str(self.file)
            )
        require_identity(state["identity"], self.identity)
        return state

    def request(self, action: str, **extra: Any) -> Mapping[str, Any]:
        self.state()
        usage = self.batch.plan.get("sourceUsage", {}).get("source", {})
        root = usage.get("root")
        if not isinstance(root, str) or usage.get("subdir") != "Maildir":
            raise MigrationError(
                "Plesk isolation requires the measured source Maildir location from this plan"
            )
        result = plesk_request(
            self.batch.source,
            "isolation",
            {
                "action": action,
                "domain": self.batch.plan["domain"],
                "identity": self.identity,
                "domains": self.batch.plan["domains"],
                "users": [row["username"] for row in self.batch.plan["mailboxes"]],
                "root": root,
                **extra,
            },
            self.batch.directory / ("plesk-isolation-" + uuid4().hex + ".txt"),
        )
        if (
            not isinstance(result, dict)
            or result.get("stateFile")
            != "/root/mailcow-in-progress/plesk-isolation/state.json"
            or type(result.get("blocked")) is not bool
        ):
            raise MigrationError(
                "Invalid Plesk isolation response; private diagnostics were retained"
            )
        return result

    def check(self, *, webmail: bool = False) -> None:
        state = self.state()
        if state is None or state["action"] != "freeze" or not state["complete"]:
            raise MigrationError(
                "Complete Plesk source isolation and retain its journal before continuing"
            )
        result = self.request("check-webmail" if webmail else "check")
        if not result["blocked"]:
            raise MigrationError("Plesk no longer reports this batch isolated")
        self.check_source()

    def check_source(self) -> None:
        manifest = read_json(self.batch.directory / "manifest.json", private=True)
        evidence = load_plan_export(
            self.configuration, self.batch, Path(manifest["export"])
        )
        current = read_plesk(
            self.batch.source,
            "export",
            self.batch.plan["domain"],
            self.batch.directory / ("plesk-source-check-" + uuid4().hex + ".txt"),
        )
        expected = read_json(evidence.data / "source.json", private=True)
        if current != expected:
            file = self.batch.directory / (
                "plesk-source-current-"
                + json_fingerprint(current, canonical=True)
                + ".json"
            )
            write_private_json(file, current, replace=True)
            raise MigrationError(
                "Plesk account/filter configuration changed; compare the retained source snapshot: "
                + str(file)
            )
        evidence.verify()

    def change(self, action: str) -> None:
        if action not in ("freeze", "release"):
            raise MigrationError("Use freeze or release for Plesk isolation")
        previous = self.state()
        if (
            previous
            and not previous["complete"]
            and previous["action"] == "release"
            and action != "release"
        ):
            raise MigrationError(
                "Finish the interrupted Plesk release before starting another isolation"
            )
        write_private_json(
            self.file,
            {"identity": self.identity, "action": action, "complete": False},
            replace=True,
        )
        result = self.request(action)
        if result["blocked"] != (action == "freeze"):
            raise MigrationError(
                "Plesk isolation result did not verify; retain the local and remote journals"
            )
        write_private_json(
            self.file,
            {
                "identity": self.identity,
                "action": action,
                "complete": True,
                "result": result,
            },
            replace=True,
        )
        print(
            "Plesk {} completed. Source recovery state: {}.".format(
                action, result["stateFile"]
            )
        )
