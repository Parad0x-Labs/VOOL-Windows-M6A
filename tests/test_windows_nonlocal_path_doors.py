"""On Windows, a share or a partial path is refused at each tool door before anything reaches for it.

A stat of `\\\\host\\share` on Windows is an SMB connection (and an NTLM hash sent to whoever
answers), so a refusal that comes after the first lookup is still the bug. The model executor is not
the only way into a tool: the fast paths cross the authorized execution boundary
(`execute_authorized_runtime_tool`), code tasks, seats and sub-tasks call the runtime door
(`execute_runtime_tool`) directly, and live-data plans and the effect gateway ask the permission
decision (`decide_tool_call`) with no door in front of it. Each of those is driven here for real, and
`tests/_nonlocal_touch_recorder.py` records (and refuses) every filesystem call or process start that
names the marker host `attkr9`.

What this file pins:

- The authorized boundary refuses a share before the Windows write gate places the write and before
  any permission decision, and also when the caller already holds an allowing decision. A cancelled
  turn still answers `cancelled` first.
- The runtime door refuses before the agent-team gate resolves the path (`containment._resolved`); a
  local path for the same bound session still reaches that gate.
- A code-task step that reads a share is refused through the executor and through the runtime door;
  a local read in the same task still runs.
- The permission decision, called the way its door-less callers call it, never resolves a share, and
  a declared write naming one is treated as an overwrite (approval in Auto), not as a create.
- A patch whose `---`/`+++` lines name a share is refused with no touch, neither in the Blackbox
  recorder's header resolution nor in the patch tool, and the journal says why.
- A call whose turn works in a share (the server's own workspace, which no request field names) is
  refused with the sentence at each of the three doors, before anything resolves that workspace.
- An MCP or plugin tool's root- or drive-relative argument still reaches its approval (the network
  tier), while a share there is refused. The declared-contract checks need a plugin pack admitted
  through the real lifecycle; a host that cannot capture a pack (Windows has no `os.O_NOFOLLOW`)
  admits none, so there they check that refusal instead and stop.
- The resolvers behind the doors, called directly as their other callers call them, never look a
  share up: the Windows write gate's move and export landings, the workspace path and root
  resolvers, and the builder's workspace-relative check.

The Windows rule is switched on only through `windows_path_policy._IS_WINDOWS` (the `windows_host`
fixture), never through `os.name` or `sys.platform`, and only after local setup, because with it on a
POSIX `tmp_path` counts as root-relative. Literal sentences are asserted, never `refusal_text`, so this
file runs against a base given only `core/windows_path_policy.py`, with no guard wired to it.
"""
from __future__ import annotations

import sys
import threading
import uuid
from pathlib import Path

import pytest

from core import authorized_tool_execution as boundary
from core import mode_permission_policy as permission
from core import runtime_execution_tools as runtime
from core import tool_intent_executor as executor
from core import windows_write_gate as gate
from core.agent_team import coordinator
from core.agent_team import gate as agent_team_gate
from core.agent_team.coordinator import TeamCoordinator
from core.council import containment
from core.hive_activity_tracker import HiveActivityTracker, HiveActivityTrackerConfig
from tests._nonlocal_touch_recorder import MARKER_FORMS, NETWORK_FORMS, MarkerForm, recorder, windows_host

STATUS = "non_local_path_refused"
_AGENT = str(Path(__file__).with_name("agent_team_agent.py"))
_LIMITS = {"max_usd": 1.0, "max_tokens": 10_000, "max_calls": 5, "wall_clock_seconds": 60}


def _sentence(form: MarkerForm) -> str:
    return "is a network or device path" if form.network else "is not a full local path"


def _spy(monkeypatch, module, name: str) -> list[tuple]:
    calls: list[tuple] = []
    real = getattr(module, name)

    def recording(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, recording)
    return calls


def _execute(intent: str, arguments: dict, source_context: dict):
    return executor.execute_tool_intent(
        {"intent": intent, "arguments": dict(arguments)},
        task_id=f"doors-{uuid.uuid4().hex}",
        session_id=str(source_context.get("session_id") or f"doors-session-{uuid.uuid4().hex}"),
        checkpoint_id=f"runtime-{uuid.uuid4().hex}",
        step_index=0,
        source_context=source_context,
        hive_activity_tracker=HiveActivityTracker(config=HiveActivityTrackerConfig(enabled=False, watcher_api_url=None)),
    )


def test_the_authorized_door_refuses_before_the_gate_and_the_decision(
    tmp_path: Path, monkeypatch, recorder, windows_host
) -> None:
    # The fast paths' door, with the Windows write gate on and one flagged folder, as on a real host.
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    flagged, workspace = tmp_path / "scratch", tmp_path / "project"
    flagged.mkdir()
    workspace.mkdir()
    monkeypatch.setenv(gate.ENV_VAR, str(flagged))
    gate_checks = _spy(monkeypatch, runtime, "windows_write_refusal_before_decision")
    decisions = _spy(monkeypatch, boundary, "decide_tool_call")
    dispatches = _spy(monkeypatch, runtime, "execute_runtime_tool")
    # What the model loop hands in when it already decided at its own controller seam.
    held = permission.PermissionDecision(
        effect=permission.PermissionEffect.ALLOW,
        mode=permission.OperatingMode.AUTO,
        actions=(permission.PermissionAction.CREATE_FILES,),
        reason="allowed by the caller's own consult",
    )
    context = {"workspace": str(workspace), "operating_mode": "auto"}
    cancelled_event = threading.Event()
    cancelled_event.set()
    windows_host.on()

    observed: dict[str, tuple[str, str, str]] = {}
    texts: dict[str, list[str]] = {}
    for form in MARKER_FORMS:
        arguments = {"path": form.value, "content": "x"}
        fresh = boundary.execute_authorized_runtime_tool("machine.write_file", arguments, source_context=context)
        handed = boundary.execute_authorized_runtime_tool(
            "machine.write_file", arguments, source_context=context, permission_decision=held
        )
        stopped = boundary.execute_authorized_runtime_tool(
            "machine.write_file", arguments, source_context={**context, "cancel_event": cancelled_event}
        )
        observed[form.id] = (fresh.status, handed.status, stopped.status)
        texts[form.id] = [fresh.response_text, handed.response_text]
        assert fresh.ok is False and handed.ok is False and stopped.ok is False
        assert fresh.details.get("executed") is False and handed.details.get("executed") is False
        assert stopped.details.get("cancelled") is True, stopped.details
    gate_checks_for_marker_forms = list(gate_checks)
    # Control: a local path still meets the write gate, which places it in the home folder (not
    # flagged) and refuses it there, before any decision.
    local = boundary.execute_authorized_runtime_tool(
        "machine.write_file", {"path": "notes.txt", "content": "x"}, source_context=context
    )

    assert recorder.touches == [] and recorder.marker_popen == [], f"{observed}\n{recorder.report()}"
    assert observed == {form.id: (STATUS, STATUS, "cancelled") for form in MARKER_FORMS}
    for form in MARKER_FORMS:
        for text in texts[form.id]:
            # The sentence a fast path relays to the user, under the boundary's own lead-in.
            assert text.startswith("`machine.write_file` was not executed: "), (form.id, text)
            assert _sentence(form) in text, (form.id, text)
    assert gate_checks_for_marker_forms == [], "the write gate placed a share before the door refused it"
    assert decisions == [], "a permission decision (an approval card) was taken for a share"
    assert dispatches == [], "a refused call crossed into the runtime door"
    assert local.status == gate.STATUS, (local.status, local.response_text)
    assert len(gate_checks) == 1, "the local control did not reach the write gate, so the check above proves nothing"


def test_the_runtime_door_refuses_before_agent_team_containment(
    tmp_path: Path, monkeypatch, recorder, windows_host
) -> None:
    # A real coordinator with an agent registered for a real claim, bound to a chat session the way the
    # coordinator binds a model agent's session. Windows refuses to start a process agent (it has no
    # confinement backend), so the coordinator's Windows policy is on here too and no process starts
    # on any host. The agent-team gate reads only the session binding and the team's workspace, so the
    # door meets it exactly as on Windows.
    monkeypatch.setattr(coordinator, "_WINDOWS_REFUSES_PROCESS_AGENTS", True)
    workspace = tmp_path / "ws"
    (workspace / "a").mkdir(parents=True)
    team = TeamCoordinator(tmp_path / "team", workspace=workspace, team_limits=_LIMITS, tick_seconds=0.25)
    session = f"openclaw:{uuid.uuid4().hex[:20]}"
    resolved = _spy(monkeypatch, containment, "_resolved")
    try:
        team.start([
            {"key": "docs", "objective": "Docs update", "importance": "low", "claims": ["a"],
             "command": [sys.executable, _AGENT, "sleeper", "20"], "limits": _LIMITS},
        ])
        docs = next(row for row in team.registry.agents() if row["key"] == "docs")
        agent_team_gate.bind_session(session, team, docs["agent_id"])
        context = {"session_id": session, "workspace": str(workspace), "workspace_root": str(workspace)}
        windows_host.on()

        results = {
            form.id: runtime.execute_runtime_tool(
                "workspace.write_file", {"path": form.value, "content": "x"}, source_context=context
            )
            for form in MARKER_FORMS
        }
        resolved_for_marker_forms = list(resolved)
        # Control: a local path for the same bound session still meets the agent-team gate.
        local = runtime.execute_runtime_tool(
            "workspace.write_file", {"path": "b/notes.md", "content": "x"}, source_context=context
        )
    finally:
        agent_team_gate.unbind_session(session)
        team.stop()
        team.close()

    observed = {form_id: result.status for form_id, result in results.items()}
    assert recorder.touches == [] and recorder.marker_popen == [], f"{observed}\n{recorder.report()}"
    assert observed == {form.id: STATUS for form in MARKER_FORMS}
    for form in MARKER_FORMS:
        result = results[form.id]
        assert result.ok is False and result.details.get("executed") is False, (form.id, result.details)
        assert _sentence(form) in result.response_text, (form.id, result.response_text)
    assert resolved_for_marker_forms == [], "agent-team containment resolved a share"
    assert local.status == agent_team_gate.GATE_STATUS, (local.status, local.response_text)
    assert len(resolved) == 1, "the local control did not reach containment, so the check above proves nothing"
    # The gate's own verdict on the local path: placed inside the team's workspace, outside every claim.
    assert str(local.details.get("reason") or "").endswith(
        "may not write b/notes.md: it is outside its claimed paths"
    ), local.details
    assert not (workspace / "b" / "notes.md").exists()


def test_a_code_task_step_reading_a_share_is_refused(tmp_path: Path, monkeypatch, recorder, windows_host) -> None:
    monkeypatch.setenv("VOOL_BLACKBOX_DIR", str(tmp_path / "blackbox"))
    monkeypatch.setenv("VOOL_CODE_TASK_DIR", str(tmp_path / "code_tasks"))
    permission.reset_mode_permission_state()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    session = f"doors-code-task-{uuid.uuid4().hex}"
    context = {"workspace": str(repo), "workspace_root": str(repo), "session_id": session, "operating_mode": "auto"}
    try:
        opened = _execute("code.task.open", {"objective": "Find why add subtracts"}, context)
        assert opened.ok, (opened.status, opened.response_text)
        task_id = str(opened.details.get("task_id") or "")
        assert task_id, opened.details
        decisions = _spy(monkeypatch, executor, "decide_tool_call")
        windows_host.on()

        observed: dict[str, tuple[str, str]] = {}
        texts: dict[str, tuple[str, str]] = {}
        for form in MARKER_FORMS:
            step = {"task_id": task_id, "intent": "workspace.read_file", "arguments": {"path": form.value}}
            through_executor = _execute("code.task.step", {**step, "step_id": f"x-{form.id}"}, context)
            through_runtime = runtime.execute_runtime_tool(
                "code.task.step", {**step, "step_id": f"r-{form.id}"}, source_context=context
            )
            observed[form.id] = (through_executor.status, through_runtime.status)
            texts[form.id] = (through_executor.response_text, through_runtime.response_text)
            assert through_executor.details.get("executed") is False, (form.id, through_executor.details)
        decisions_for_marker_forms = list(decisions)
        # Control: the same task still reads a local file, through the model's door.
        local = _execute(
            "code.task.step",
            {"task_id": task_id, "step_id": "local", "intent": "workspace.read_file", "arguments": {"path": "calc.py"}},
            context,
        )
    finally:
        permission.reset_mode_permission_state()

    assert recorder.touches == [] and recorder.marker_popen == [], f"{observed}\n{recorder.report()}"
    assert observed == {form.id: (STATUS, STATUS) for form in MARKER_FORMS}
    for form in MARKER_FORMS:
        for text in texts[form.id]:
            assert _sentence(form) in text, (form.id, text)
    assert decisions_for_marker_forms == [], "a permission decision was taken for a step that reads a share"
    assert local.ok, (local.status, local.response_text)
    assert len(decisions) == 1, "the local control did not reach the permission decision"


def _a_pack_can_be_captured_here(probe: Path) -> bool:
    """Whether this host can capture a plugin pack at all. Windows has no `os.O_NOFOLLOW`, so it cannot,
    and then no pack is ever admitted and no plugin tool exists."""
    from core.plugin_package import PackageError, capture_package

    probe.mkdir()
    try:
        capture_package(probe)
    except PackageError as exc:
        return "unavailable on this host" not in str(exc)
    finally:
        probe.rmdir()
    return True


@pytest.fixture
def declared_write_contract(tmp_path: Path, monkeypatch):
    """A third-party contract that DECLARES a workspace write (`pack.touch`), admitted through the
    real plugin lifecycle and loaded into the one registry, as tests/test_plugin_lifecycle.py does."""
    from core import plugin_tools, tool_registry
    from core.plugin_lifecycle import LifecycleError
    from core.runtime_flags import override
    from tests._toolchain_fixtures import PLUGIN_ID, make_plugin, reset_toolchain_state

    monkeypatch.setenv("VOOL_PLUGIN_LIFECYCLE_PATH", str(tmp_path / "lifecycle.json"))
    monkeypatch.setenv("VOOL_PLUGINS_DIR", str(tmp_path))
    monkeypatch.setenv("VOOL_HOME", str(tmp_path / "home"))
    # make_plugin writes this into os.environ itself; set it here first so teardown restores it.
    monkeypatch.setenv("VOOL_PLUGIN_SCRATCH_ROOT", str(tmp_path / "scratch"))
    reset_toolchain_state()
    if not _a_pack_can_be_captured_here(tmp_path / "capture-probe"):
        # No plugin tool can exist on this host, so there is no declared contract to call. What is checked
        # is that the pack is refused; on a host that gains the capability the pack is admitted below.
        with pytest.raises(LifecycleError, match="could not be captured"):
            make_plugin(tmp_path, admit=True)
        reset_toolchain_state()
        pytest.skip("this host cannot capture a plugin pack, so it admits none and has no plugin tool to call")
    make_plugin(tmp_path, admit=True)
    tool_registry.reset()
    with override("plugin_runtime_tools", True):
        plugin_tools.load_all(tmp_path)
    yield f"{PLUGIN_ID}.touch"
    reset_toolchain_state()


def _permission_decider(workspace: Path):
    """`decide_tool_call` called as live_data_plan.py and effect_gateway.py call it: no door runs first."""

    def decide(intent: str, arguments: dict, mode: str) -> permission.PermissionDecision:
        return permission.decide_tool_call(
            intent=intent,
            arguments=arguments,
            task_id=f"doors-{uuid.uuid4().hex}",
            source_context={"workspace": str(workspace), "operating_mode": mode, "session_id": "doors-permission"},
        )

    return decide


def test_permission_side_callers_never_resolve_a_share(tmp_path: Path, monkeypatch, recorder, windows_host) -> None:
    # The helpers behind the decision use the network tier, so only network forms are driven.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    decide = _permission_decider(workspace)
    permission.reset_mode_permission_state()

    try:
        windows_host.on()
        decisions: dict[tuple[str, str, str, str], permission.PermissionDecision] = {}
        for form in NETWORK_FORMS:
            for key in ("path", "target", "destination"):
                for intent in ("workspace.write_file", "machine.write_file", "pdf.extract_text"):
                    arguments = {key: form.value, **({"content": "x"} if intent.endswith(".write_file") else {})}
                    for mode in ("manual", "auto"):
                        decisions[(intent, key, form.id, mode)] = decide(intent, arguments, mode)
    finally:
        permission.reset_mode_permission_state()

    assert recorder.touches == [] and recorder.marker_popen == [], recorder.report()
    assert all(isinstance(decision, permission.PermissionDecision) for decision in decisions.values())


def test_a_declared_write_naming_a_share_is_asked_as_an_overwrite_without_touch(
    tmp_path: Path, monkeypatch, recorder, windows_host, declared_write_contract: str
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    decide = _permission_decider(workspace)
    permission.reset_mode_permission_state()

    try:
        windows_host.on()
        # Control: a declared write to a new local file is a create, which Auto allows. Anything
        # stricter below is the share, not the contract.
        create = decide(declared_write_contract, {"path": "new.txt"}, "auto")
        decisions: dict[tuple[str, str], permission.PermissionDecision] = {}
        for form in NETWORK_FORMS:
            for key in ("path", "target", "destination"):
                decisions[(key, form.id)] = decide(declared_write_contract, {key: form.value}, "auto")
    finally:
        permission.reset_mode_permission_state()

    declared = {
        (key, form_id): (decision.effect, permission.PermissionAction.OVERWRITE_EXISTING_FILES in decision.actions)
        for (key, form_id), decision in decisions.items()
    }
    assert recorder.touches == [] and recorder.marker_popen == [], f"{declared}\n{recorder.report()}"
    assert create.effect is permission.PermissionEffect.ALLOW, (create.effect, create.actions, create.reason)
    # A target that cannot be checked is treated as one that exists: an overwrite, asked in Auto.
    assert declared == {
        (key, form.id): (permission.PermissionEffect.REQUIRE_APPROVAL, True)
        for key in ("path", "target", "destination")
        for form in NETWORK_FORMS
    }


@pytest.mark.parametrize("form", [pytest.param(form, id=form.id) for form in NETWORK_FORMS])
def test_a_patch_header_naming_a_share_is_refused_without_touch(
    tmp_path: Path, monkeypatch, recorder, windows_host, form: MarkerForm
) -> None:
    # `a/` + `//attkr9/s/x` is `a///attkr9/s/x`, which strips to the share. (`a//attkr9` would strip
    # to `/attkr9`, a root-relative name the network tier does not refuse.)
    monkeypatch.setenv("VOOL_BLACKBOX_DIR", str(tmp_path / "blackbox"))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # As on a Windows host: the write gate is on and the chat's workspace is a flagged disposable
    # folder, so the gate lets the patch through to the recorder and the patch tool.
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    monkeypatch.setenv(gate.ENV_VAR, str(workspace))
    context = {"workspace": str(workspace), "workspace_root": str(workspace), "session_id": f"doors-{uuid.uuid4().hex}"}
    patch = f"--- a/{form.value}.txt\n+++ b/{form.value}.txt\n@@ -1 +1 @@\n-a\n+b\n"
    windows_host.on()

    result = runtime.execute_runtime_tool("workspace.apply_unified_diff", {"patch": patch}, source_context=context)

    assert recorder.touches == [] and recorder.marker_popen == [], f"{result.status}: {result.response_text}\n{recorder.report()}"
    assert result.ok is False, (result.status, result.response_text)
    assert "is a network or device path" in result.response_text, result.response_text
    # The flight recorder stayed in front of the patch tool and journaled the header as unresolved,
    # for the same reason, without resolving it.
    from core.blackbox.store import default_store

    intended = [
        entry for entry in default_store().entries()
        if entry.get("kind") == "effect_intended" and entry.get("intent") == "workspace.apply_unified_diff"
    ]
    assert intended, "the Blackbox recorder did not see the patch"
    assert all(entry.get("path_unresolved") is True for entry in intended), intended
    assert all("is a network or device path" in str(entry.get("unresolved_reason") or "") for entry in intended), intended


@pytest.mark.parametrize("form", [pytest.param(form, id=form.id) for form in NETWORK_FORMS])
def test_a_turn_whose_workspace_is_a_share_is_refused_at_every_door(
    tmp_path: Path, monkeypatch, recorder, windows_host, form: MarkerForm
) -> None:
    # As on a Windows host: the write gate is on, so a machine write is placed before any decision.
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    monkeypatch.setenv(gate.ENV_VAR, str(tmp_path / "scratch"))
    local = tmp_path / "ws"
    local.mkdir()
    (local / "notes.txt").write_text("local notes", encoding="utf-8")
    decisions = _spy(monkeypatch, executor, "decide_tool_call")
    gate_checks = _spy(monkeypatch, runtime, "windows_write_refusal_before_decision")
    windows_host.on()

    shared = {"workspace": form.value, "workspace_root": form.value, "operating_mode": "auto"}
    answers = {
        "executor": _execute("workspace.read_file", {"path": "notes.txt"}, shared),
        "runtime door": runtime.execute_runtime_tool("workspace.read_file", {"path": "notes.txt"}, source_context=shared),
        "authorized door": boundary.execute_authorized_runtime_tool(
            "machine.write_file", {"path": "notes.txt", "content": "x"}, source_context=shared
        ),
    }
    refused_decisions, refused_gate_checks = len(decisions), len(gate_checks)
    # Control: the same read in a local workspace still runs.
    read = _execute("workspace.read_file", {"path": "notes.txt"}, {"workspace": str(local), "operating_mode": "auto"})

    assert recorder.touches == [] and recorder.marker_popen == [], recorder.report()
    for door, answer in answers.items():
        assert (answer.status, answer.ok) == (STATUS, False), (door, answer.status, answer.response_text)
        assert f"`{form.value}` is a network or device path" in str(answer.response_text or ""), (door, answer.response_text)
    assert (refused_decisions, refused_gate_checks) == (0, 0), "the share workspace reached a decision or the write gate"
    assert (read.status, read.ok) == ("executed", True) and "local notes" in str(read.response_text or ""), read.response_text


def test_an_mcp_or_plugin_tool_keeps_its_partial_paths_but_not_a_share(
    tmp_path: Path, monkeypatch, recorder, windows_host, declared_write_contract: str
) -> None:
    from core.runtime_flags import override

    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    monkeypatch.setenv(gate.ENV_VAR, str(workspace))
    decisions = _spy(monkeypatch, executor, "decide_tool_call")
    context = {"workspace": str(workspace), "operating_mode": "manual"}
    windows_host.on()

    with override("plugin_runtime_tools", True):
        partial = {value: _execute(declared_write_contract, {"path": value}, context) for value in ("\\notes\\x.txt", "C:notes\\x.txt")}
        asked = len(decisions)
        share = _execute(declared_write_contract, {"path": "//attkr9/s/x.txt"}, context)

    assert recorder.touches == [] and recorder.marker_popen == [], recorder.report()
    # A plugin's path means what its server says: only the network tier applies, so these reach the
    # operator's approval as on base ...
    assert {value: answer.status for value, answer in partial.items()} == dict.fromkeys(partial, "pending_approval"), {
        value: (answer.status, answer.response_text) for value, answer in partial.items()
    }
    assert asked == 2
    # ... and a share is refused before any decision.
    assert (share.status, (share.details or {}).get("kind")) == (STATUS, "unc"), share.response_text
    assert len(decisions) == asked


@pytest.mark.parametrize(
    ("intent", "arguments"),
    [
        pytest.param("machine.move_path", {"source": "//attkr9/s/a.txt", "destination": "ws/b.txt"}, id="move-from-share"),
        pytest.param("machine.move_path", {"source": "ws/a.txt", "destination": "//attkr9/s/b.txt"}, id="move-to-share"),
        pytest.param("media.export", {"project_id": "clip", "output_path": "//attkr9/s/o.mp4"}, id="export-to-share"),
    ],
)
def test_the_write_gate_asked_directly_never_places_a_share(
    tmp_path: Path, monkeypatch, recorder, windows_host, intent: str, arguments: dict
) -> None:
    # The gate is a public predicate its callers ask directly, ahead of any door: its landing for a
    # move (`machine_file_ops._canonicalize`) and for an export (`export_destination`) must not
    # resolve a share to find out where it lands.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.txt").write_text("a", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    environ = {gate.ENV_VAR: str(workspace)}
    windows_host.on()

    reason = gate.refusal(intent, arguments, workspace, windows=True, environ=environ)
    # Control: the same move inside the flagged workspace is let through.
    local = gate.refusal("machine.move_path", {"source": "ws/a.txt", "destination": "ws/b.txt"}, workspace, windows=True, environ=environ)

    assert recorder.touches == [] and recorder.marker_popen == [], recorder.report()
    assert reason, "the gate let a write to or from a share through"
    assert local is None, local


@pytest.mark.parametrize("form", [pytest.param(form, id=form.id) for form in NETWORK_FORMS])
def test_the_workspace_resolvers_never_resolve_a_share_root(tmp_path: Path, recorder, windows_host, form: MarkerForm) -> None:
    # Code tasks, RepoOps and the Blackbox recorder hand their stored root straight to the path
    # resolver; a team start and a runtime context hand theirs to the root resolver. The doors refuse a
    # turn's share workspace before either is reached; these stand behind them for those callers.
    from core.execution.workspace_tools import resolve_workspace_path
    from core.runtime_paths import resolve_workspace_root

    local = tmp_path / "ws"
    local.mkdir()
    windows_host.on()

    with pytest.raises(ValueError, match="is a network or device path"):
        resolve_workspace_path("notes.txt", workspace_root=Path(form.value))
    with pytest.raises(ValueError, match="is a network or device path"):
        resolve_workspace_root(form.value)
    resolved = resolve_workspace_path("notes.txt", workspace_root=local)
    root = resolve_workspace_root(str(local))

    assert recorder.touches == [] and recorder.marker_popen == [], recorder.report()
    assert resolved == (local / "notes.txt").resolve() and root == local.resolve()


def test_the_builder_never_resolves_a_share_to_place_a_named_file(tmp_path: Path, recorder, windows_host) -> None:
    from core.agent_runtime.builder.named_file_build import workspace_relative_path

    local = tmp_path / "ws"
    local.mkdir()
    windows_host.on()

    placed = {
        "rooted name in a share workspace": workspace_relative_path("/docs/notes.md", workspace_root="\\\\attkr9\\s\\you\\Desktop"),
        "share in a local workspace": workspace_relative_path("//attkr9/s/notes.md", workspace_root=str(local)),
        "device path in a local workspace": workspace_relative_path("//?/UNC/attkr9/s/notes.md", workspace_root=str(local)),
        # Control: a rooted path inside the local workspace is still resolved into it.
        "local": workspace_relative_path(str(local / "docs" / "notes.md"), workspace_root=str(local)),
    }

    assert recorder.touches == [] and recorder.marker_popen == [], recorder.report()
    assert placed == {
        "rooted name in a share workspace": "",
        "share in a local workspace": "",
        "device path in a local workspace": "",
        "local": "docs/notes.md",
    }
