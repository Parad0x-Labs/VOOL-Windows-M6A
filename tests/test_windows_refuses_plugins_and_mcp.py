"""0.7.1 on Windows: VOOL starts no plugin and no MCP server, and says so in plain words.

The stated limit is "VOOL doesn't start plugins or MCP servers on Windows in 0.7.1; use
WSL2/Linux". Each row drives one entry point with the Windows decision switched on (the module
switch, or the sandbox backend's own platform check) and every process start wired to fail the
test, so a row passes only when the refusal comes before anything is spawned, the VoolConfine
launcher's probe and grant included.

Two findings this pins:
- a plugin or MCP call on Windows with a working launcher raised `KernelIsolationUnavailableError`
  (a ValueError) out of `run_plugin_tool` and `MCPStdioClient.start`, which caught only
  `ConfinementUnavailableError`;
- an MCP server whose command is bare `python` passed the sandbox's phase-1 program gate, so a
  python-launched MCP server could start.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from core import plugin_executor
from core.execution import mcp_bridge
from core.mcp_client import MCPError, MCPStdioClient
from sandbox import job_runner

REFUSAL = "VOOL doesn't start plugins or MCP servers on Windows in 0.7.1; use WSL2/Linux"


class _ProcessStarted(AssertionError):
    pass


def _no_process_may_start(monkeypatch) -> None:
    def refuse(*args, **kwargs):
        raise _ProcessStarted(f"a process was started: {args[:1]!r}")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "call", refuse)
    monkeypatch.setattr(subprocess, "check_call", refuse)
    monkeypatch.setattr(subprocess, "check_output", refuse)


def _backend_must_not_be_asked(monkeypatch) -> None:
    # On Windows the backend probes and grants through the launcher, which is a process start.
    def asked(*args, **kwargs):
        raise _ProcessStarted("the confinement backend was asked (it probes the launcher on Windows)")

    monkeypatch.setattr(plugin_executor, "_kernel_confinement_prefix", asked)


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(plugin_executor, "_WINDOWS_REFUSES_PLUGINS_AND_MCP", True, raising=False)
    _no_process_may_start(monkeypatch)
    _backend_must_not_be_asked(monkeypatch)
    yield


@pytest.fixture(autouse=True)
def _isolate_mcp(monkeypatch):
    monkeypatch.delenv("VOOL_MCP_CONFIG", raising=False)
    mcp_bridge.reset_mcp_state()
    yield
    mcp_bridge.reset_mcp_state()


def _handler(tmp_path: Path) -> Path:
    root = tmp_path / "plugin"
    (root / "bin").mkdir(parents=True)
    script = root / "bin" / "run.py"
    script.write_text("print('{}')\n", encoding="utf-8")
    script.chmod(0o755)
    return root


def _run(root: Path, **extra):
    return plugin_executor.run_plugin_tool(
        intent="pack.echo",
        arguments={"text": "hi"},
        handler={"entry": "bin/run.py"},
        schema={"type": "object", "properties": {"text": {"type": "string"}}},
        plugin_root=root,
        **extra,
    )


# --- plugin executor ----------------------------------------------------------------------------


def test_the_plugin_executor_refuses_on_windows_before_any_process(tmp_path, windows) -> None:
    result = _run(_handler(tmp_path), scratch_dir=str(tmp_path / "scratch"))
    assert result.ok is False
    assert REFUSAL in result.error
    assert result.confinement == ""
    assert not (tmp_path / "scratch").exists(), "nothing is prepared for a refused plugin"


def test_heuristic_only_cannot_open_the_door_on_windows(tmp_path, windows) -> None:
    result = _run(_handler(tmp_path), confinement="heuristic_only")
    assert result.ok is False and REFUSAL in result.error


def test_the_backends_own_windows_check_also_refuses(tmp_path, monkeypatch) -> None:
    # The module switch is off; the sandbox backend says Windows. Either one refuses.
    monkeypatch.setattr(plugin_executor, "_WINDOWS_REFUSES_PLUGINS_AND_MCP", False, raising=False)
    monkeypatch.setattr(job_runner, "_is_windows_platform", lambda: True)
    _no_process_may_start(monkeypatch)
    _backend_must_not_be_asked(monkeypatch)
    result = _run(_handler(tmp_path))
    assert result.ok is False and REFUSAL in result.error


def test_confined_argv_refuses_on_windows_without_asking_the_backend(tmp_path, windows) -> None:
    with pytest.raises(plugin_executor.ConfinementUnavailableError, match="doesn't start plugins"):
        plugin_executor.confined_argv([sys.executable, "server.py"], writable_roots=(tmp_path,))


# --- KernelIsolationUnavailableError never escapes -------------------------------------------------


def _phase1_gate(argv, **kwargs):
    # What the Windows backend does with a plugin handler: phase 1 runs python/pytest only, and a
    # handler script path is neither, so it raises KernelIsolationUnavailableError (a ValueError).
    job_runner.JobRunner._windows_phase1_argv(list(argv))
    raise AssertionError("the phase-1 gate accepted a plugin handler")


def test_a_kernel_isolation_refusal_is_a_structured_plugin_result_not_an_exception(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(plugin_executor, "_WINDOWS_REFUSES_PLUGINS_AND_MCP", False, raising=False)
    # Off Windows: the refusal also asks the sandbox backend's own platform check, which a Windows
    # host answers from the platform; both switches off, as in test_other_hosts_see_no_refusal.
    monkeypatch.setattr(job_runner, "_is_windows_platform", lambda: False)
    monkeypatch.setattr(plugin_executor, "_kernel_confinement_prefix", _phase1_gate)
    _no_process_may_start(monkeypatch)
    result = _run(_handler(tmp_path))  # used to raise KernelIsolationUnavailableError
    assert result.ok is False
    assert result.status == "confinement_unavailable"
    assert "not started" in result.error


def test_a_kernel_isolation_refusal_is_an_mcp_error_not_a_value_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(plugin_executor, "_WINDOWS_REFUSES_PLUGINS_AND_MCP", False, raising=False)
    monkeypatch.setattr(plugin_executor, "_kernel_confinement_prefix", _phase1_gate)
    _no_process_may_start(monkeypatch)
    client = MCPStdioClient("node", ["server.js"], cwd=str(tmp_path))
    with pytest.raises(MCPError):
        client.start()


def test_on_windows_a_kernel_isolation_refusal_becomes_the_plain_refusal(tmp_path, monkeypatch) -> None:
    # The backend raises on Windows; the result the operator sees is the stated refusal.
    calls = {"n": 0}

    def refusal_after_first_ask() -> str:
        calls["n"] += 1
        return "" if calls["n"] == 1 else plugin_executor.WINDOWS_PLUGIN_MCP_REFUSAL

    def raises(*args, **kwargs):
        raise job_runner.KernelIsolationUnavailableError("Windows confinement could not grant")

    monkeypatch.setattr(plugin_executor, "windows_plugin_mcp_refusal", refusal_after_first_ask, raising=False)
    monkeypatch.setattr(plugin_executor, "_kernel_confinement_prefix", raises)
    _no_process_may_start(monkeypatch)
    with pytest.raises(plugin_executor.ConfinementUnavailableError) as caught:
        plugin_executor.confined_argv(["tool"], writable_roots=(tmp_path,))
    assert REFUSAL in str(caught.value)


# --- MCP client -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    ["python", "python3", "pythonw", "py", "python.exe", "PYTHON", sys.executable, "node", "C:\\Python312\\python.exe"],
)
def test_the_mcp_client_refuses_every_server_on_windows(tmp_path, windows, command) -> None:
    from core.mcp_client import MCPRefusedOnWindows

    client = MCPStdioClient(command, ["-m", "server"], cwd=str(tmp_path / "cwd"))
    with pytest.raises(MCPRefusedOnWindows) as caught:
        client.start()
    assert REFUSAL in str(caught.value)
    assert client._proc is None
    assert not (tmp_path / "cwd").exists(), "nothing is prepared for a refused server"


def test_a_python_launched_mcp_server_passes_the_phase1_gate_so_the_client_must_refuse_first() -> None:
    # The gap: phase 1 accepts a bare `python` (it becomes this interpreter), so the backend alone
    # would have confined and started a python MCP server on Windows.
    program = job_runner.JobRunner._windows_phase1_argv(["python", "server.py"])
    assert program[1:] == ["server.py"]


# --- MCP bridge (boot listing, skill discovery, calls) ---------------------------------------------


def _python_server_config(tmp_path: Path, monkeypatch) -> None:
    cfg = tmp_path / "mcp_servers.json"
    cfg.write_text(json.dumps({"servers": [{"name": "py", "command": "python", "args": ["server.py"]}]}),
                   encoding="utf-8")
    monkeypatch.setenv("VOOL_MCP_CONFIG", str(cfg))
    mcp_bridge.reset_mcp_state()


def test_the_bridge_lists_no_tools_and_keeps_the_plain_refusal(tmp_path, monkeypatch, windows) -> None:
    _python_server_config(tmp_path, monkeypatch)
    assert mcp_bridge.mcp_contracts() == []
    assert mcp_bridge.server_failure_reason("py") == plugin_executor.WINDOWS_PLUGIN_MCP_REFUSAL


def test_an_mcp_call_on_windows_answers_the_plain_refusal(tmp_path, monkeypatch, windows) -> None:
    _python_server_config(tmp_path, monkeypatch)
    result = mcp_bridge.execute_mcp_intent("mcp.py.echo", {"text": "hi"})
    assert result.ok is False
    assert result.status == "mcp_refused_on_windows"
    assert result.response_text == plugin_executor.WINDOWS_PLUGIN_MCP_REFUSAL


def test_the_explained_absence_of_an_mcp_tool_is_the_plain_refusal(tmp_path, monkeypatch, windows) -> None:
    from core import tool_intent_executor

    _python_server_config(tmp_path, monkeypatch)
    explained = tool_intent_executor._undispatchable_execution("mcp.py.echo", {"text": "hi"})
    assert explained.ok is False
    assert explained.response_text == plugin_executor.WINDOWS_PLUGIN_MCP_REFUSAL
    assert explained.user_safe_response_text == plugin_executor.WINDOWS_PLUGIN_MCP_REFUSAL


def test_mcp_skill_discovery_starts_no_server_on_windows(tmp_path, monkeypatch, windows) -> None:
    from core import native_skill_library

    _python_server_config(tmp_path, monkeypatch)
    loaded = native_skill_library.load_mcp_skill_contracts()
    assert tuple(loaded.contracts) == ()


# --- plugin dispatch and the add-ons enable step ---------------------------------------------------


@pytest.fixture
def admitted_pack(tmp_path, monkeypatch):
    from tests._toolchain_fixtures import make_plugin, reset_toolchain_state

    monkeypatch.setenv("VOOL_PLUGIN_LIFECYCLE_PATH", str(tmp_path / "lifecycle.json"))
    monkeypatch.setenv("VOOL_PLUGINS_DIR", str(tmp_path))
    monkeypatch.setenv("VOOL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("VOOL_PLUGIN_SCRATCH_ROOT", str(tmp_path / "scratch"))
    plugin_dir = make_plugin(tmp_path, admit=True)
    reset_toolchain_state()
    yield plugin_dir
    reset_toolchain_state()


def test_a_registered_plugin_tool_answers_the_plain_refusal_on_windows(admitted_pack, tmp_path, monkeypatch) -> None:
    from core import plugin_tools, tool_intent_executor, tool_registry
    from core.runtime_flags import override

    tool_registry.reset()
    plugin_tools.reset_roots()
    with override("plugin_runtime_tools", True):
        plugin_tools.load_all(tmp_path)
        contract = tool_registry.tool_for_intent("pack.echo")
        assert contract is not None
        monkeypatch.setattr(plugin_executor, "_WINDOWS_REFUSES_PLUGINS_AND_MCP", True, raising=False)
        _no_process_may_start(monkeypatch)
        _backend_must_not_be_asked(monkeypatch)
        captured: list[object] = []
        import core.plugin_package as plugin_package

        monkeypatch.setattr(plugin_package, "capture_package", lambda *a, **k: captured.append(a))
        direct = plugin_tools.execute_plugin_contract(contract, {"text": "hi"})
        assert direct.ok is False and REFUSAL in direct.error
        assert captured == [], "the package snapshot is not copied for a refused plugin"
        answer = tool_intent_executor._execute_plugin_tool("pack.echo", {"text": "hi"})
    assert answer is not None and answer.ok is False
    assert answer.response_text == plugin_executor.WINDOWS_PLUGIN_MCP_REFUSAL
    assert answer.user_safe_response_text == plugin_executor.WINDOWS_PLUGIN_MCP_REFUSAL
    assert answer.details.get("executed") is False


def test_enabling_a_plugin_with_tools_is_refused_on_windows(tmp_path, monkeypatch) -> None:
    from core import plugin_lifecycle
    from tests._toolchain_fixtures import make_plugin

    monkeypatch.setenv("VOOL_PLUGIN_LIFECYCLE_PATH", str(tmp_path / "lifecycle.json"))
    monkeypatch.setenv("VOOL_PLUGINS_DIR", str(tmp_path))
    monkeypatch.setenv("VOOL_HOME", str(tmp_path / "home"))
    pack = make_plugin(tmp_path, admit=False)
    plugin_lifecycle.install("pack", root=pack, source="test-fixture")
    plugin_lifecycle.verify("pack", root=pack)
    monkeypatch.setattr(plugin_executor, "_WINDOWS_REFUSES_PLUGINS_AND_MCP", True, raising=False)
    _no_process_may_start(monkeypatch)
    with pytest.raises(plugin_lifecycle.LifecycleError) as caught:
        plugin_lifecycle.enable("pack")
    assert REFUSAL in str(caught.value)
    assert plugin_lifecycle.record_for("pack").enabled is False


def test_an_instruction_only_pack_still_enables_on_windows(tmp_path, monkeypatch) -> None:
    # A skills-only add-on starts nothing, so the Windows limit does not reach it.
    from core import plugin_lifecycle
    from tests._toolchain_fixtures import make_plugin

    monkeypatch.setenv("VOOL_PLUGIN_LIFECYCLE_PATH", str(tmp_path / "lifecycle.json"))
    monkeypatch.setenv("VOOL_PLUGINS_DIR", str(tmp_path))
    monkeypatch.setenv("VOOL_HOME", str(tmp_path / "home"))
    pack = make_plugin(tmp_path, tools=[], manifest_extra={"runtime": None}, admit=False)
    plugin_lifecycle.install("pack", root=pack, source="test-fixture")
    plugin_lifecycle.verify("pack", root=pack)
    monkeypatch.setattr(plugin_executor, "_WINDOWS_REFUSES_PLUGINS_AND_MCP", True, raising=False)
    _no_process_may_start(monkeypatch)
    assert plugin_lifecycle.enable("pack").enabled is True


# --- other hosts are unchanged ---------------------------------------------------------------------


def test_other_hosts_see_no_refusal(monkeypatch) -> None:
    monkeypatch.setattr(plugin_executor, "_WINDOWS_REFUSES_PLUGINS_AND_MCP", False, raising=False)
    monkeypatch.setattr(job_runner, "_is_windows_platform", lambda: False)
    assert plugin_executor.windows_plugin_mcp_refusal() == ""
    seen: list[list[str]] = []

    def backend(argv, **kwargs):
        seen.append(list(argv))
        return ["wrapper", *argv]

    monkeypatch.setattr(plugin_executor, "_kernel_confinement_prefix", backend)
    argv, label = plugin_executor.confined_argv(["python", "server.py"], writable_roots=(Path("."),))
    assert argv == ["wrapper", "python", "server.py"] and label == "kernel:wrapper"
