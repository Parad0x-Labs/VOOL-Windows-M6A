"""On Windows an add-on enables only when its manifest is provably skills-only.

0.7.1 starts no plugin and no MCP server on Windows. A skills-only add-on starts nothing and still
enables; anything else is refused whole, with the plain refusal. "Skills-only" is decided fail
closed: every manifest key outside an explicit known-safe list refuses, a runnable key refuses
(`tools` and `runtime` are tolerated only when empty, as VOOL's own skills installs write them),
and a manifest that cannot be read as a JSON object refuses.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from core import plugin_executor, plugin_lifecycle

REFUSAL = "VOOL doesn't start plugins or MCP servers on Windows in 0.7.1; use WSL2/Linux"


def _no_process_may_start(monkeypatch) -> None:
    def refuse(*args, **kwargs):
        raise AssertionError(f"a process was started: {args[:1]!r}")

    for name in ("Popen", "run", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, refuse)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("VOOL_PLUGIN_LIFECYCLE_PATH", str(tmp_path / "lifecycle.json"))
    monkeypatch.setenv("VOOL_PLUGINS_DIR", str(tmp_path))
    monkeypatch.setenv("VOOL_HOME", str(tmp_path / "home"))
    return tmp_path


def _skills_pack(root: Path, extra: dict | None = None, *, pack_id: str = "skillpack") -> Path:
    pack = root / "plugins" / pack_id
    (pack / ".codex-plugin").mkdir(parents=True)
    (pack / "skills" / "notes").mkdir(parents=True)
    (pack / "skills" / "notes" / "SKILL.md").write_text(
        "---\nname: notes\ndescription: Take notes.\n---\n# Notes\n", encoding="utf-8"
    )
    manifest = {
        "name": pack_id,
        "version": "1.0.0",
        "description": "Instruction-only pack.",
        "author": {"name": "someone"},
        "interface": {"displayName": "Notes", "shortDescription": "Take notes."},
        "keywords": ["notes"],
        "skills": "./skills/",
        "permissions": [],
        "credential_bindings": [],
        "requires": [],
        "vool": {"origin": "user"},
        "tools": [],
        "runtime": None,
        **dict(extra or {}),
    }
    (pack / ".codex-plugin" / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    return pack


def _verified(pack: Path, pack_id: str = "skillpack") -> None:
    plugin_lifecycle.install(pack_id, root=pack, source="test-fixture")
    plugin_lifecycle.verify(pack_id, root=pack)


def _windows(monkeypatch) -> None:
    monkeypatch.setattr(plugin_executor, "_WINDOWS_REFUSES_PLUGINS_AND_MCP", True, raising=False)
    _no_process_may_start(monkeypatch)


def _assert_refused(pack_id: str = "skillpack") -> None:
    with pytest.raises(plugin_lifecycle.LifecycleError) as caught:
        plugin_lifecycle.enable(pack_id)
    assert REFUSAL in str(caught.value)
    assert plugin_lifecycle.record_for(pack_id).enabled is False


def test_a_pure_skills_pack_still_enables_on_windows(store, monkeypatch) -> None:
    pack = _skills_pack(store)
    _verified(pack)
    _windows(monkeypatch)
    assert plugin_lifecycle.enable("skillpack").enabled is True


@pytest.mark.parametrize(
    "key, value",
    [
        ("tools", [{"name": "run", "handler": {"entry": "bin/run"}}]),
        ("runtime", {"contract_version": 1}),
        ("commands", ["./commands/"]),
        ("hooks", {"PreToolUse": [{"command": "x"}]}),
        ("mcpServers", {"srv": {"command": "python", "args": ["s.py"]}}),
        ("mcp_servers", [{"command": "python"}]),
        ("lspServers", {"py": {"command": "pylsp"}}),
        ("monitors", [{"command": "watch"}]),
        ("bin", "./bin/"),
        ("scripts", {"postinstall": "python setup.py"}),
        ("apps", ["./app"]),
        ("agents", ["./agents/"]),
    ],
)
def test_a_runnable_key_refuses_the_whole_addon_on_windows(store, monkeypatch, key, value) -> None:
    pack = _skills_pack(store, {key: value})
    _verified(pack)
    _windows(monkeypatch)
    _assert_refused()


@pytest.mark.parametrize("key", ["commands", "hooks", "mcpServers", "lspServers", "monitors", "bin", "scripts"])
def test_a_runnable_key_refuses_even_when_empty(store, monkeypatch, key) -> None:
    # Only `tools` and `runtime`, which VOOL's own skills installs write empty, are tolerated empty.
    pack = _skills_pack(store, {key: []})
    _verified(pack)
    _windows(monkeypatch)
    _assert_refused()


def test_an_unknown_key_refuses_on_windows(store, monkeypatch) -> None:
    pack = _skills_pack(store, {"homepage": "https://example.invalid"})
    _verified(pack)
    _windows(monkeypatch)
    _assert_refused()


def test_an_unreadable_manifest_refuses_on_windows(store, monkeypatch) -> None:
    pack = _skills_pack(store)
    _verified(pack)
    (pack / ".codex-plugin" / "plugin.json").write_text("{not json", encoding="utf-8")
    _windows(monkeypatch)
    _assert_refused()


def test_the_safe_list_decision_itself_fails_closed(tmp_path) -> None:
    # The same decision the enable step uses, on shapes a verify would never let through.
    from core.plugin_lifecycle import _declares_runnable_code

    manifest_dir = tmp_path / "p" / ".codex-plugin"
    manifest_dir.mkdir(parents=True)
    path = manifest_dir / "plugin.json"
    for text in ("{not json", "[]", '"skills"', "null"):
        path.write_text(text, encoding="utf-8")
        assert _declares_runnable_code(tmp_path / "p") is True, text
    path.unlink()
    assert _declares_runnable_code(tmp_path / "p") is True, "a missing manifest"
    path.write_text(json.dumps({"name": "p", "version": "1", "skills": "./skills/"}), encoding="utf-8")
    assert _declares_runnable_code(tmp_path / "p") is False
    path.write_text(json.dumps({"name": "p", "homepage": "x"}), encoding="utf-8")
    assert _declares_runnable_code(tmp_path / "p") is True


def test_other_hosts_enable_as_before(store, monkeypatch) -> None:
    monkeypatch.setattr(plugin_executor, "_WINDOWS_REFUSES_PLUGINS_AND_MCP", False, raising=False)
    from sandbox import job_runner

    monkeypatch.setattr(job_runner, "_is_windows_platform", lambda: False)
    pack = _skills_pack(store, {"homepage": "https://example.invalid", "hooks": {"x": 1}})
    _verified(pack)
    assert plugin_lifecycle.enable("skillpack").enabled is True
