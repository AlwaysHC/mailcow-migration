import pkgutil
import re

from ..core.errors import MigrationError


def read_resource(name: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+\.(?:json|sh|php|py|awk)", name):
        raise MigrationError("Invalid bundled resource name")
    data = pkgutil.get_data(__name__, name)
    if data is None:
        raise MigrationError("Bundled resource was not found: {}".format(name))
    return data.decode("utf-8").replace("\r\n", "\n")


def php_resource(name: str) -> str:
    helper = read_resource(name)
    if not helper.startswith("<?php\n"):
        raise MigrationError("Invalid packaged PHP helper: " + name)
    return helper[6:]
