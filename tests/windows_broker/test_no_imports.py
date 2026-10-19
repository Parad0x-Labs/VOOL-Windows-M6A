"""The phase-A broker package stays disconnected from runtime code."""

from __future__ import annotations

import ast
from pathlib import Path


def test_no_production_module_imports_windows_broker() -> None:
    root = Path(__file__).resolve().parents[2]
    for folder in (root / "core", root / "apps", root / "installer"):
        if not folder.exists():
            continue
        for path in folder.rglob("*.py"):
            if "windows_broker" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    assert not module.startswith("core.windows_broker"), path
                elif isinstance(node, ast.Import):
                    assert all(
                        not alias.name.startswith("core.windows_broker")
                        for alias in node.names
                    ), path

