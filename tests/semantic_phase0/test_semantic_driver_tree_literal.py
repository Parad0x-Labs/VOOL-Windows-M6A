"""The differential driver names its tree as a Python literal, whatever characters the path holds.

Steve job 41 (5 Nov 2026) on Windows: test_proof_subprocess_freezes_disk_capacity_for_both_trees
failed with "frozen_disk_probe: driver produced no result". The driver pasted the tree path between
double quotes, so ``C:\\Users\\...`` became the escape ``\\U`` and the child died on a SyntaxError
before it ran a single turn. A path with a backslash or a double quote must reach the child intact.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "semantic_phase0_base_differential.py"


def _differential():
    spec = importlib.util.spec_from_file_location("vool_base_differential_tree_literal", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stub_tree(root: Path) -> Path:
    for package in ("apps", "core", "tests"):
        (root / package).mkdir(parents=True)
        (root / package / "__init__.py").write_text("", encoding="utf-8")
    (root / "apps" / "vool_agent.py").write_text(
        "class _Loader: pass\n"
        "class VoolAgent:\n"
        "    def __init__(self, **kwargs): self.context_loader = _Loader()\n"
        "    def start(self): pass\n"
        "    def run_once(self, text, **kwargs):\n"
        "        return {'response': 'echo:' + text}\n",
        encoding="utf-8",
    )
    (root / "core" / "policy_engine.py").write_text("_POLICY_CACHE = {}\ndef load(): return {}\n", encoding="utf-8")
    (root / "tests" / "conftest.py").write_text("def make_stub_context(): return {}\n", encoding="utf-8")
    return root


@pytest.mark.parametrize("name", [r"Users\Unicode", 'say "hi"', r"trailing\\"])
def test_the_driver_runs_in_a_tree_whose_path_needs_escaping(tmp_path, name) -> None:
    module = _differential()
    tree = _stub_tree(tmp_path / name)

    rows = module._run_corpus(tree, "tree_literal", ["hello"])

    assert rows[0]["raised"] == ""
    assert rows[0]["fields"]["response"] == repr("echo:hello")
