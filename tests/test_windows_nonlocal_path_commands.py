"""On Windows, a registry command whose filesystem field names a share is refused before its handler runs.

A stat of `\\\\host\\share` on Windows is an SMB connection (and an NTLM hash sent to whoever
answers), and a `git` started with its working directory on a share is the same connection.
Measured on the base (f8d2e30), every command below whose handler ran reached for its path: the
archaeology commands resolve `workspace_root`, `path` and the files they compare
(`project_archaeology`), a team start resolves its `workspace`, a council convene starts
`git rev-parse` there, the rollback resolves both expected install paths, a bypass grant resolves
the workspace it is bound to, and `logs.verify` joins `segment_id` onto its segment folder, where a
share re-roots the join. (`blackbox.rollback` stops at its own availability probe when no turn is
recorded; its field is refused first all the same.)

Every projection reaches one seam, `execute_command`, so that is where the refusal is pinned,
through three of its doors: the served `/api/commands/dispatch` (the owner command centre), the
served `/api/update/rollback` press (a legacy route forwarded into the registry), and the model's
`operator.command.*` tool call in the real executor. The answer is the registry's own usage fault
(HTTP 400) whose detail names the field and the reason `non_local_path_refused`, and whose summary is
the sentence the user reads. `project_archaeology.compare` is also called directly, which is the
defence behind the registry: it answers `ScopeRefused` without reading the share.

`tests/_nonlocal_touch_recorder.py` records (and refuses) every filesystem call or process start
naming the marker host `attkr9`. The Windows rule is switched on only through
`windows_path_policy._IS_WINDOWS` (the `windows_host` fixture), and only after local setup, because
with it on a POSIX `tmp_path` counts as root-relative; the local values here are therefore relative
to the working directory (`ws`). Literal sentences are asserted, never `refusal_text`, so this file
runs unchanged against a base that has no such module.

Not pinned here: `archaeology.compare` with `kind="files"` reads an absolute path outside
`workspace_root` on every OS. That is a separate, cross-platform finding; only the share forms are
asserted below.
"""
from __future__ import annotations

import importlib
import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from core import mode_permission_policy as permission
from core import project_archaeology, runtime_paths
from core import tool_intent_executor as executor
from core.agent_team import service as agent_teams
from core.command_registry.model_tools import project_model_tools
from core.command_registry.registry import registry
from core.hive_activity_tracker import HiveActivityTracker, HiveActivityTrackerConfig
from core.updater import runtime as updater_runtime
from tests._nonlocal_touch_recorder import MARKER_FORMS, NETWORK_FORMS, MarkerForm, recorder, windows_host
from tests.asgi_harness import asgi_request

STATUS = "non_local_path_refused"
# What the page's own fetches send: JSON from the page's loopback origin.
JSON_HEADERS = {"Content-Type": "application/json", "Host": "127.0.0.1", "Origin": "http://127.0.0.1:11435"}
# The server-side press the update routes build from a real UI click (core/web/api/service.py).
PRESS = {"gesture": "web-ui-press", "origin": "web-ui"}
TEAM_LIMITS = {"max_usd": 1.0, "max_tokens": 10_000, "max_calls": 5, "wall_clock_seconds": 60}
# Cloud seats, so a convene passes the council's local-model wall (as in tests/test_council_api.py).
CLOUD_SEATS = [
    {"role_id": "builder", "model": "vendor/model-a", "votes": True},
    {"role_id": "falsifier", "model": "vendor/model-b", "votes": True},
    {"role_id": "reviewer", "model": "vendor/model-c", "votes": True},
]
# The share spellings that stay a share after `Path(workspace) / name` on every host: pathlib keeps
# the two leading slashes of a POSIX path, and a Windows path re-roots on any of them. (A backslash
# spelling is a share on Windows but only an odd file name under the workspace on Linux.)
JOINED_SHARES = ("//attkr9/s/x.txt", "//?/UNC/attkr9/s/x.txt", "//./pipe/attkr9")


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("VOOL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("VOOL_WORKSPACE_ROOT", str(tmp_path / "default-workspace"))
    runtime_paths.configure_runtime_home(tmp_path / "home")
    yield
    runtime_paths.configure_runtime_home(None)


def _sentence(form: MarkerForm, value: str) -> str:
    # What the user is told, with the value they gave: a share or device by the network tier, a
    # root- or drive-relative path by the full rule.
    return f"`{value}` " + ("is a network or device path" if form.network else "is not a full local path")


def _file_on(form: MarkerForm, name: str) -> str:
    """`name` inside the form's folder, in the form's own separator, so the kind does not change."""
    return form.value + ("\\" if "\\" in form.value else "/") + name


def _local_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`ws` under the working directory, holding two small files to compare.

    The commands are given `ws`, never `tmp_path` itself: with the Windows rule on, a POSIX absolute
    path is root-relative, so only a relative name is a local value on both hosts.
    """
    workspace = tmp_path / "ws"
    (workspace / "docs").mkdir(parents=True)
    (workspace / "a.txt").write_text("first line\nsecond line\n", encoding="utf-8")
    (workspace / "b.txt").write_text("first line\nchanged line\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return workspace


def _app():
    from apps.vool_api_server import create_app
    from core.web.api.runtime import RuntimeServices

    return create_app(RuntimeServices(display_name="VOOL"))


def _post(app, path: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    status, _headers, raw = asgi_request(
        app, method="POST", path=path, headers=JSON_HEADERS, body=json.dumps(payload).encode("utf-8")
    )
    return status, json.loads(raw.decode("utf-8") or "{}")


def _dispatch(app, command_id: str, input_data: dict[str, Any], approval_context: dict | None = None):
    body: dict[str, Any] = {"command_id": command_id, "input": input_data}
    if approval_context is not None:
        body["approval_context"] = approval_context
    return _post(app, "/api/commands/dispatch", body)


def _spy_handlers(monkeypatch: pytest.MonkeyPatch, command_ids) -> dict[str, list]:
    """A recording pass-through on each command's own handler, where the registry resolves it."""
    runs: dict[str, list] = {}
    for command_id in dict.fromkeys(command_ids):
        spec = registry().lookup(command_id)
        assert spec is not None and spec.handler is not None, command_id
        module_name, _, attribute = spec.handler.dotted.partition(":")
        module = importlib.import_module(module_name)
        real = getattr(module, attribute)
        seen = runs.setdefault(command_id, [])

        def recording(inp, ctx, *, _real=real, _seen=seen):
            _seen.append(inp)
            return _real(inp, ctx)

        monkeypatch.setattr(module, attribute, recording)
    return runs


def _reached(recorder, touches_from: int, starts_from: int) -> dict[str, list[str]]:
    """Marker touches and marker process starts since the counts one request began with."""
    def where(frames: tuple[str, ...]) -> str:
        return next((frame for frame in reversed(frames) if not frame.startswith("tests/")), "(no VOOL frame)")

    return {
        "touches": [f"{touch.op} {touch.path!r} at {where(touch.frames)}" for touch in recorder.touches[touches_from:]],
        "process starts": [
            f"argv={list(spawn.argv)!r} cwd={spawn.cwd!r} at {where(spawn.frames)}"
            for spawn in recorder.popen[starts_from:]
            if spawn.names_marker
        ],
    }


def _fault_fields(envelope: dict[str, Any]) -> dict[str, Any]:
    """The parts of a command envelope a refusal is read from: its fault code, and the reason, field
    and kind of path its detail names."""
    fault = envelope.get("fault") or {}
    detail = fault.get("detail") or {}
    return {
        "ok": envelope.get("ok"),
        "fault": fault.get("code"),
        "reason": detail.get("reason"),
        "argument": detail.get("argument"),
        "kind": detail.get("kind"),
    }


def _refusal(status: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {"status": status, **_fault_fields(payload)}


def _differences(observed: dict[str, dict], expected: dict[str, dict]) -> str:
    """One line per request that did not go as expected, naming only the fields that differ."""
    lines = []
    for request, fields in observed.items():
        wrong = {name: value for name, value in fields.items() if value != expected[request][name]}
        if wrong:
            lines.append(f"{request}: {wrong}")
    return "\n".join(lines)


def _command_table(session: str) -> list[tuple[str, str, dict[str, Any], dict[str, Any] | None]]:
    """Every filesystem field of a registry command: (command, field, the rest of the request, the
    approval context the command's own gate needs).

    The rest of each request is local and gets past the command's own gate, so on a build without the
    refusal each one reaches its handler where it can (archaeology, a team start, a convene, the
    rollback, a bypass grant, a segment check). `blackbox.rollback` cannot: with no recorded turn it
    stops at its availability probe. Two requests are deliberately left one step short of running
    work, so that a build without the refusal reaches for the path and then stops on its own: the
    team start has no agents (it opens the chat's team, then refuses the empty plan, so no agent
    process starts) and the convene has no problem (it runs `git rev-parse` in the workspace, then
    refuses, so no council thread starts).
    """
    return [
        ("archaeology.search", "workspace_root", {"text": "notes"}, None),
        ("archaeology.search", "path_prefix", {"text": "notes", "workspace_root": "ws"}, None),
        ("archaeology.trace", "workspace_root", {"turn_id": "turn-1"}, None),
        ("archaeology.trace", "path", {"workspace_root": "ws"}, None),
        ("archaeology.recovery_plan", "workspace_root", {"reference": "a.txt"}, None),
        ("archaeology.recovery_plan", "path", {"workspace_root": "ws"}, None),
        ("archaeology.compare", "workspace_root", {"kind": "files", "a": "a.txt", "b": "b.txt"}, None),
        ("archaeology.compare", "a", {"kind": "files", "b": "b.txt", "workspace_root": "ws"}, None),
        ("archaeology.compare", "b", {"kind": "files", "a": "a.txt", "workspace_root": "ws"}, None),
        ("archaeology.locate", "workspace_root", {"reference": "a.txt"}, None),
        ("agents.start", "workspace", {"session_id": session, "agents": [], "team_limits": TEAM_LIMITS}, PRESS),
        ("council.convene", "workspace_root", {"payload": {"problem": "", "seats": CLOUD_SEATS}}, None),
        ("update.rollback", "expected_app_path", {}, PRESS),
        ("update.rollback", "expected_data_dir", {}, PRESS),
        ("logs.verify", "segment_id", {}, None),
        # A chat-scoped, until-off grant: the one bypass bound to a workspace. The test mints its
        # single-use confirmation for each request, as the local UI mints one before the press.
        ("approvals.bypass.activate", "workspace_root", {"session_id": session, "scope": "session", "until_off": True}, None),
        ("blackbox.rollback", "workspace_root", {"turn_id": "turn-1"}, None),
    ]


def test_command_paths_naming_a_share_are_refused_before_the_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorder, windows_host
) -> None:
    _local_workspace(tmp_path, monkeypatch)
    session = f"openclaw:{uuid.uuid4().hex[:20]}"
    table = _command_table(session)
    handler_runs = _spy_handlers(monkeypatch, [command_id for command_id, _field, _rest, _press in table])
    # The segment check is a projection-lane command; the lane is on in the product and pinned off
    # for the suite (tests/conftest.py), so it is switched back on here, in this test's own folder.
    monkeypatch.setenv("VOOL_LIQUEFY_LOGS", "1")
    # Updates stay off the network, and this test's update service is its own.
    monkeypatch.setenv("VOOL_UPDATE_DISABLED", "1")
    monkeypatch.setattr(updater_runtime, "_SUBSYSTEM", None)
    monkeypatch.setattr(permission, "_bypass_grants_path", lambda: tmp_path / "authority" / "bypass_grants.json")
    permission.reset_mode_permission_state()
    # One single-use bypass confirmation per request, minted for that exact grant, as the local UI
    # mints one before the press.
    minted = {
        form.id: permission.request_bypass_confirmation(
            session_id=session, scope="session", until_off=True, workspace_root=form.value
        )
        for form in MARKER_FORMS
    }
    teams_before = set(agent_teams._TEAMS)
    app = _app()
    windows_host.on()

    observed: dict[str, dict] = {}
    expected: dict[str, dict] = {}
    try:
        for form in MARKER_FORMS:
            for command_id, field, rest, press in table:
                input_data = {**rest, field: form.value}
                if command_id == "approvals.bypass.activate":
                    input_data["confirmation_id"] = minted[form.id]
                key = f"{command_id} {field} {form.id}"
                touches, starts = len(recorder.touches), len(recorder.popen)
                runs = len(handler_runs[command_id])
                status, payload = _dispatch(app, command_id, input_data, press)
                observed[key] = {
                    **_refusal(status, payload),
                    "says what and why": _sentence(form, form.value) in str(payload.get("summary") or ""),
                    "handler runs": len(handler_runs[command_id]) - runs,
                    **_reached(recorder, touches, starts),
                }
                expected[key] = {
                    "status": 400,
                    "ok": False,
                    "fault": "usage",
                    "reason": STATUS,
                    "argument": field,
                    "kind": form.kind,
                    "says what and why": True,
                    "handler runs": 0,
                    "touches": [],
                    "process starts": [],
                }
        # Control: the same command with local, relative values for the same fields still runs.
        control_runs = len(handler_runs["archaeology.search"])
        control = _dispatch(app, "archaeology.search", {"text": "notes", "workspace_root": "ws", "path_prefix": "docs"})
        control_ran = len(handler_runs["archaeology.search"]) - control_runs
    finally:
        # Without the refusal, the team start opens a team (with its own loop thread) and the
        # bypass is granted; neither may outlive this test.
        with agent_teams._LOCK:
            started_here = [agent_teams._TEAMS.pop(name) for name in set(agent_teams._TEAMS) - teams_before]
        for team in started_here:
            team.close()
        with permission._LOCK:
            for confirmation in minted.values():
                permission._PENDING_BYPASS_CONFIRMATIONS.pop(confirmation, None)
        permission.reset_mode_permission_state()

    assert observed == expected, _differences(observed, expected)
    assert recorder.touches == [] and recorder.marker_popen == [], recorder.report()
    assert control[0] == 200 and control[1].get("ok") is True, control
    assert control_ran == 1, "the local control did not reach the handler, so the checks above prove nothing"


def test_update_rollback_route_refuses_a_share(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorder, windows_host
) -> None:
    monkeypatch.chdir(tmp_path)
    # Updates stay off the network, and this test's update service is its own.
    monkeypatch.setenv("VOOL_UPDATE_DISABLED", "1")
    monkeypatch.setattr(updater_runtime, "_SUBSYSTEM", None)
    handler_runs = _spy_handlers(monkeypatch, ["update.rollback"])["update.rollback"]
    rollbacks: list[tuple] = []
    real_press = updater_runtime.UpdateSubsystem.press_rollback

    def recording_press(self, *args, **kwargs):
        rollbacks.append(args)
        return real_press(self, *args, **kwargs)

    monkeypatch.setattr(updater_runtime.UpdateSubsystem, "press_rollback", recording_press)
    app = _app()
    windows_host.on()

    # The installer's rollback (installer/update_cli.py) posts both expected paths, from its --app-path
    # and data folder, to the running app; the route forwards each one into the registry as given.
    shares: dict[str, tuple[dict[str, str], tuple[str, ...]]] = {
        "both on a share": (
            {"expected_app_path": "//attkr9/s/app", "expected_data_dir": "//attkr9/s/d"},
            ("`//attkr9/s/app` is a network or device path", "`//attkr9/s/d` is a network or device path"),
        ),
    }
    for form in MARKER_FORMS:
        for field, name in (("expected_app_path", "app"), ("expected_data_dir", "d")):
            value = _file_on(form, name)
            shares[f"{field} {form.id}"] = ({field: value}, (_sentence(form, value),))
    answers = {request: _post(app, "/api/update/rollback", body) for request, (body, _told) in shares.items()}
    runs_for_shares = len(handler_runs)
    # Control: a local relative app path still reaches the rollback handler, which answers its own
    # 409 (not this installation) without starting a rollback.
    control = _post(app, "/api/update/rollback", {"expected_app_path": "app"})

    assert recorder.touches == [] and recorder.marker_popen == [], f"{answers}\n{recorder.report()}"
    for request, (status, payload) in answers.items():
        _body, told = shares[request]
        assert (status, payload.get("ok")) == (400, False), (request, status, payload)
        fault = payload.get("fault") or {}
        assert (fault.get("code"), (fault.get("detail") or {}).get("reason")) == ("usage", STATUS), (request, payload)
        assert any(sentence in str(payload.get("summary") or "") for sentence in told), (request, payload)
    assert runs_for_shares == 0, "the rollback handler ran for an install path on a share"
    assert rollbacks == [], "a rollback was started"
    assert control == (409, {"ok": False}), control
    assert len(handler_runs) == 1, "the local control did not reach the handler, so the checks above prove nothing"


def _execute(intent: str, arguments: dict, source_context: dict):
    return executor.execute_tool_intent(
        {"intent": intent, "arguments": dict(arguments)},
        task_id=f"nonlocal-{uuid.uuid4().hex}",
        session_id=f"nonlocal-session-{uuid.uuid4().hex}",
        checkpoint_id=f"runtime-{uuid.uuid4().hex}",
        step_index=0,
        source_context=source_context,
        hive_activity_tracker=HiveActivityTracker(config=HiveActivityTrackerConfig(enabled=False, watcher_api_url=None)),
    )


def test_archaeology_compare_of_a_file_on_a_share_is_refused_for_model_and_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorder, windows_host
) -> None:
    workspace = _local_workspace(tmp_path, monkeypatch)
    # The command is model-offerable: the model is offered it as operator.command.archaeology.compare.
    project_model_tools()
    handler_runs = _spy_handlers(monkeypatch, ["archaeology.compare"])["archaeology.compare"]
    app = _app()
    context = {"workspace": str(workspace), "operating_mode": "auto"}
    windows_host.on()

    observed: dict[str, dict] = {}
    expected: dict[str, dict] = {}
    for form in NETWORK_FORMS:
        share = _file_on(form, "x.txt")
        for side, arguments in (
            ("a", {"kind": "files", "a": share, "b": "b.txt", "workspace_root": "ws"}),
            ("b", {"kind": "files", "a": "a.txt", "b": share, "workspace_root": "ws"}),
        ):
            refused = {
                "ok": False,
                "fault": "usage",
                "reason": STATUS,
                "argument": side,
                "kind": form.kind,
                "says what and why": True,
                "handler runs": 0,
                "touches": [],
                "process starts": [],
            }
            # The model's tool call, through the real executor in Auto mode: the registry's usage
            # fault is the tool's status, and its summary is what the model relays ...
            touches, starts, runs = len(recorder.touches), len(recorder.popen), len(handler_runs)
            execution = _execute("operator.command.archaeology.compare", arguments, context)
            envelope = (execution.details or {}).get("envelope") or {}
            observed[f"model {side} {form.id}"] = {
                "tool status": execution.status,
                **_fault_fields(envelope),
                "ok": execution.ok,
                "says what and why": _sentence(form, share) in str(execution.response_text or ""),
                "handler runs": len(handler_runs) - runs,
                **_reached(recorder, touches, starts),
            }
            expected[f"model {side} {form.id}"] = {"tool status": "usage", **refused}
            # ... and the owner command centre's dispatch.
            touches, starts, runs = len(recorder.touches), len(recorder.popen), len(handler_runs)
            status, payload = _dispatch(app, "archaeology.compare", arguments)
            observed[f"dispatch {side} {form.id}"] = {
                **_refusal(status, payload),
                "says what and why": _sentence(form, share) in str(payload.get("summary") or ""),
                "handler runs": len(handler_runs) - runs,
                **_reached(recorder, touches, starts),
            }
            expected[f"dispatch {side} {form.id}"] = {"status": 400, **refused}

    # The authority itself, called directly: the defence behind the registry. Its `files` compare
    # joins each name onto the workspace, and a share re-roots that join.
    for share in JOINED_SHARES:
        touches, starts = len(recorder.touches), len(recorder.popen)
        try:
            project_archaeology.compare(kind="files", a=share, b="b.txt", workspace_root="ws", requester="test")
        except Exception as exc:
            answer: Exception | None = exc
        else:
            answer = None
        observed[f"direct {share}"] = {
            "raised": type(answer).__name__,
            "says why": "a network or device path" in str(getattr(answer, "reason", "")),
            **_reached(recorder, touches, starts),
        }
        expected[f"direct {share}"] = {"raised": "ScopeRefused", "says why": True, "touches": [], "process starts": []}

    # Controls: two local files in the same relative workspace are still compared, by every door.
    local = {"kind": "files", "a": "a.txt", "b": "b.txt", "workspace_root": "ws"}
    model_local = _execute("operator.command.archaeology.compare", local, context)
    dispatch_local = _dispatch(app, "archaeology.compare", local)
    direct_local = project_archaeology.compare(**local, requester="test")

    assert observed == expected, _differences(observed, expected)
    assert recorder.touches == [] and recorder.marker_popen == [], recorder.report()
    assert (model_local.status, model_local.ok) == ("executed", True), model_local.response_text
    assert "file compare a.txt vs b.txt: changed" in str(model_local.response_text or ""), model_local.response_text
    assert dispatch_local[0] == 200 and dispatch_local[1].get("ok") is True, dispatch_local
    for compared in (dispatch_local[1]["data"], direct_local):
        assert compared["comparison"]["summary"] == "file compare a.txt vs b.txt: changed", compared["comparison"]
