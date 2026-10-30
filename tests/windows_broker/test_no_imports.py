"""The broker package stays disconnected from runtime code.

Phase B starts and stops the broker with the VOOL API, so apps/vool_api_server.py may import
core.windows_broker.process, and nothing else may. Nothing is routed through the broker yet: any
other use, the client from any file included, still fails.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = "core.windows_broker"
#: The one production use phase B allows: the API server starting and stopping the broker.
ALLOWED = {"apps/vool_api_server.py": "core.windows_broker.process"}


def _python_files() -> list[Path]:
    """Every Python file in the repository, outside the tests and the broker package itself."""
    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z", "--", "*.py"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout.decode("utf-8").split("\0")
        paths = [ROOT / name for name in listed if name]
    except (OSError, subprocess.CalledProcessError):
        skip = {".git", ".venv", "venv", "node_modules", "__pycache__", "build", "dist"}
        paths = [p for p in ROOT.rglob("*.py") if not skip.intersection(p.relative_to(ROOT).parts)]
    files = []
    for path in paths:
        parts = path.relative_to(ROOT).parts
        if parts[0] == "tests" or parts[:2] == ("core", "windows_broker"):
            continue
        files.append(path)
    return files


def _package_of(path: Path) -> list[str]:
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    return parts if parts[-1] == "__init__" and parts.pop() is not None else parts[:-1]


def _uses_of_the_broker(path: Path, source: bytes | None = None) -> list[str]:
    """Uses of the broker package in ``path`` (or in ``source``, read as if it sat at ``path``)."""
    try:
        tree = ast.parse(path.read_bytes() if source is None else source, filename=str(path))
    except SyntaxError:
        # Not Python 3 that this repository runs (vendored or archived text): it cannot import.
        return []
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = _package_of(path)
                base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
                module = ".".join([*base, *([node.module] if node.module else [])])
            else:
                module = node.module or ""
            names = [module, *(f"{module}.{alias.name}" for alias in node.names)]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # importlib.import_module("core.windows_broker"), __import__, and the like.
            names = [node.value] if "windows_broker" in node.value else []
            found.extend(f"string {value!r} at line {node.lineno}" for value in names)
            continue
        else:
            continue
        found.extend(
            f"import {name} at line {node.lineno}"
            for name in names
            if name == PACKAGE or name.startswith(PACKAGE + ".")
        )
    return found


def test_no_production_module_uses_windows_broker() -> None:
    files = _python_files()
    # The walk reaches the runtime, the tools and the installer, not just one folder.
    assert any(path.relative_to(ROOT).parts[0] == "core" for path in files)
    assert any(path.relative_to(ROOT).parts[0] == "ops" for path in files)
    uses = {
        path.relative_to(ROOT).as_posix(): hits for path in files if (hits := _uses_of_the_broker(path))
    }
    allowed = {name: [hit for hit in uses.pop(name, []) if not _is_allowed(name, hit)] for name in ALLOWED}
    assert uses == {}
    assert allowed == {name: [] for name in ALLOWED}


def _is_allowed(name: str, hit: str) -> bool:
    """A plain import of the allowed module, or of a name from it, in the file allowed to use it."""
    module = ALLOWED[name]
    if not hit.startswith("import "):
        return False
    imported = hit[len("import ") :].split(" at line ")[0]
    return imported == module or imported.startswith(module + ".")


def test_the_api_server_starts_the_broker_through_the_allowed_module() -> None:
    # The exception is not stale: the API server does import the start module.
    for name, module in ALLOWED.items():
        hits = _uses_of_the_broker(ROOT / name)
        assert any(_is_allowed(name, hit) for hit in hits), (name, module, hits)


def test_only_the_allowed_module_is_allowed() -> None:
    name = "apps/vool_api_server.py"
    probe = ROOT / name
    forms = {
        "from core.windows_broker.process import start_for_api": True,
        "import core.windows_broker.process": True,
        "from core.windows_broker import process": False,
        "from core.windows_broker.client import BrokerClient": False,
        "import core.windows_broker": False,
        "import importlib; importlib.import_module('core.windows_broker.process')": False,
    }
    for source, expected in forms.items():
        hits = _uses_of_the_broker(probe, source.encode("utf-8") + b"\n")
        assert hits, source
        assert all(_is_allowed(name, hit) for hit in hits) == expected, (source, hits)


def test_every_import_form_is_seen() -> None:
    # Each form is read as if it sat in core/, so relative imports resolve against core.
    probe = ROOT / "core" / "_no_imports_probe.py"
    forms = {
        "import core.windows_broker.client": True,
        "from core import windows_broker": True,
        "from core.windows_broker import client": True,
        "from .windows_broker import client": True,
        "from . import windows_broker": True,
        "import importlib; importlib.import_module('core.windows_broker')": True,
        "import core.windows_brokerage": False,
        "from core import windows": False,
    }
    for source, expected in forms.items():
        hits = _uses_of_the_broker(probe, source.encode("utf-8") + b"\n")
        assert bool(hits) == expected, (source, hits)
