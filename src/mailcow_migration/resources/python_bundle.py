import importlib
import sys
import tempfile
from pathlib import Path


def run(sources, module):
    with tempfile.TemporaryDirectory(prefix="mailcow-migration-") as directory:
        package = Path(directory) / "mailcow_helpers"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        for name, source in sources.items():
            (package / name).write_text(source, encoding="utf-8")
        sys.path.insert(0, directory)
        try:
            importlib.import_module(f"{package.name}.{module}").run()
        finally:
            sys.path.remove(directory)
