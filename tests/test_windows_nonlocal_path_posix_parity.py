"""Off Windows, the Windows non-local path refusal never answers: POSIX keeps the answers it had.

On Windows, `//attkr9/s` is a share, and reaching for it is an SMB connection, so the other
`test_windows_nonlocal_path_*` files pin that VOOL refuses it before any lookup. On Linux and macOS
the same text is an ordinary absolute path: two leading slashes read as one, and an automounter may
serve `//host/share` style paths. Every guard the fix adds therefore returns at once when
`windows_path_policy._IS_WINDOWS` is False, before it reads anything, and POSIX must keep exactly the
answers it had.

These tests pin that on the real surfaces, with the seam explicitly off:

- the served `/api/chat`, `/api/projects`, `/api/files/raw` and `/api/commands/dispatch` routes
  (`apps.vool_api_server.create_app` driven by `tests/asgi_harness.asgi_request`);
- the real executor (`execute_tool_intent`) running a workspace read and a `browser.render`;
- the browser engine's own entry points, which the search provider and media ingestion call directly.

The expected answers were measured on the base (f8d2e30), where no guard exists yet. A guard that
forgets its short-circuit changes one of them: a 400 or a refusal sentence where the base ran the
turn, compared the files or started the browser, or a lookup that silently stops happening. Literal
strings are asserted, never `refusal_text`, so this file runs unchanged against a base that has no
such module.

`tests/_nonlocal_touch_recorder.py` refuses every lookup naming the marker host `attkr9`, so a run as
root can never create `/attkr9`; on Linux a refused lookup answers what the missing path answers. The
fake browser runs outside the recorder, because the URL in its argv names the marker and the recorder
would refuse that process start.
"""
from __future__ import annotations

import importlib
import json
import ntpath
import os
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from core import generated_files, project_store, runtime_paths, windows_path_policy
from core import tool_intent_executor as executor
from core.command_registry.registry import registry
from core.hive_activity_tracker import HiveActivityTracker, HiveActivityTrackerConfig
from tests._nonlocal_touch_recorder import Recorder, record_touches, windows_host
from tests.asgi_harness import asgi_request
from tools.browser import browser_render

# On Windows these spellings are shares, and the other test_windows_nonlocal_path_* files pin the refusal.
pytestmark = pytest.mark.skipif(os.name == "nt", reason="pins the POSIX answers; on Windows these paths are shares")

HOST = {"Host": "127.0.0.1"}
# What the page's own fetches send: JSON from the page's loopback origin.
JSON_HEADERS = {"Content-Type": "application/json", "Host": "127.0.0.1", "Origin": "http://127.0.0.1:11435"}
PNG = b"\x89PNG\r\n\x1a\nnot-really-an-image"
# Nothing the Windows refusal says may appear in a POSIX answer.
REFUSAL_WORDS = ("non_local_path_refused", "is a network or device path", "is not a full local path")
# The page the fake browser prints, and what the engine makes of it.
FAKE_TITLE = "Rendered off Windows"
FAKE_PAGE = f"<html><head><title>{FAKE_TITLE}</title></head><body>rendered off windows</body></html>"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("VOOL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("VOOL_WORKSPACE_ROOT", str(tmp_path / "default-workspace"))
    runtime_paths.configure_runtime_home(tmp_path / "home")
    yield
    runtime_paths.configure_runtime_home(None)


def _app(agent: Any = None):
    from apps.vool_api_server import create_app
    from core.web.api.runtime import RuntimeServices

    return create_app(RuntimeServices(display_name="VOOL", agent=agent))


def _post(app, path: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    status, _headers, raw = asgi_request(
        app, method="POST", path=path, headers=JSON_HEADERS, body=json.dumps(payload).encode("utf-8")
    )
    return status, json.loads(raw.decode("utf-8") or "{}")


def _execute(intent: str, arguments: dict, source_context: dict):
    return executor.execute_tool_intent(
        {"intent": intent, "arguments": dict(arguments)},
        task_id=f"parity-{uuid.uuid4().hex}",
        session_id=f"parity-session-{uuid.uuid4().hex}",
        checkpoint_id=f"runtime-{uuid.uuid4().hex}",
        step_index=0,
        source_context=source_context,
        hive_activity_tracker=HiveActivityTracker(config=HiveActivityTrackerConfig(enabled=False, watcher_api_url=None)),
    )


def _spy(monkeypatch: pytest.MonkeyPatch, owner: object, name: str) -> list[dict]:
    calls: list[dict] = []
    real = getattr(owner, name)

    def recording(*args, **kwargs):
        calls.append(kwargs or {"args": args})
        return real(*args, **kwargs)

    monkeypatch.setattr(owner, name, recording)
    return calls


def _spy_handler(monkeypatch: pytest.MonkeyPatch, command_id: str) -> list[dict]:
    """A recording pass-through on a command's own handler, where the registry resolves it."""
    spec = registry().lookup(command_id)
    assert spec is not None and spec.handler is not None, command_id
    module_name, _, attribute = spec.handler.dotted.partition(":")
    return _spy(monkeypatch, importlib.import_module(module_name), attribute)


def _refusal_words(*answers: Any) -> list[str]:
    """The refusal words found anywhere in these answers: an empty list when there are none."""
    text = "\n".join(answer if isinstance(answer, str) else json.dumps(answer, default=str) for answer in answers)
    return [word for word in REFUSAL_WORDS if word in text]


def _looked_up_in(recorder: Recorder, touches_from: int) -> set[str]:
    """The VOOL files that looked the marker path up since `touches_from`.

    Off Windows these lookups are the base code path running: a guard that forgot its short-circuit
    would answer before them, and they would silently stop happening. Files, not lines, so an
    unrelated edit above a lookup does not move the pin.
    """
    files = set()
    for touch in recorder.touches[touches_from:]:
        frame = next((frame for frame in reversed(touch.frames) if not frame.startswith("tests/")), "")
        files.add(frame.rpartition(":")[0] or "(no VOOL frame)")
    return files


def _local_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`ws` under the working directory, holding a file to read and two files to compare."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("local notes", encoding="utf-8")
    (workspace / "a.txt").write_text("first line\nsecond line\n", encoding="utf-8")
    (workspace / "b.txt").write_text("first line\nchanged line\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return workspace


def _fake_browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A harmless executable pinned as the browser: it writes its argv down and prints a small page.

    Playwright is made unimportable, as on a machine without it, so a render takes the Chrome
    fallback: the lane that starts a browser process with the URL in its argv. Answers the argv log:
    one line per start, each argument followed by US (0x1f).
    """
    log = tmp_path / "browser-argv.log"
    binary = tmp_path / "fake-browser"
    binary.write_text(
        "#!/bin/sh\n"
        f"for arg in \"$@\"; do printf '%s\\037' \"$arg\" >> '{log}'; done\n"
        f"printf '\\n' >> '{log}'\n"
        f"printf '%s' '{FAKE_PAGE}'\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    monkeypatch.setenv("VOOL_BROWSER_BINARY", str(binary))
    monkeypatch.setenv("PLAYWRIGHT_ENABLED", "1")
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    browser_render.reset_browser_binary_cache_for_test()
    assert browser_render.find_browser_binary() == str(binary)
    return log


def _browser_argvs(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [line.split("\x1f")[:-1] for line in log.read_text(encoding="utf-8").splitlines()]


def test_off_windows_every_surface_answers_as_on_base(
    tmp_path: Path, tmp_path_factory, monkeypatch: pytest.MonkeyPatch, windows_host, enable_web
) -> None:
    # `enable_web`: the web tools are off under pytest; the shipped default turns them on.
    from apps import vool_api_server
    from apps.vool_agent import VoolAgent

    workspace = _local_workspace(tmp_path, monkeypatch)
    agent = VoolAgent(backend_name="test-backend", device="posix-parity-test", persona_id="default")
    # The served app hands each turn to the agent the server booted with; record every turn it is handed.
    monkeypatch.setattr(vool_api_server, "_agent", agent)
    turns: list[str] = []
    real_run_agent = vool_api_server._run_agent

    def recording_run_agent(user_text: str, **kwargs: Any) -> dict[str, Any]:
        turns.append(user_text)
        return real_run_agent(user_text, **kwargs)

    monkeypatch.setattr(vool_api_server, "_run_agent", recording_run_agent)
    decisions = _spy(monkeypatch, executor, "decide_tool_call")
    compare_runs = _spy_handler(monkeypatch, "archaeology.compare")
    app = _app(agent)
    browser_log = _fake_browser(tmp_path, monkeypatch)
    # Real Linux or macOS: the seam is off, and set off explicitly so nothing earlier can leave it on.
    windows_host.off()
    assert windows_path_policy._IS_WINDOWS is False

    with record_touches(monkeypatch, tmp_root=tmp_path_factory.getbasetemp()) as recorder:
        # A programmatic chat whose explicit workspace is a POSIX //host/share path.
        chat_status, chat = _post(
            app, "/api/chat",
            {
                "messages": [{"role": "user", "content": "hello from a posix share"}],
                "session_id": f"openclaw:{uuid.uuid4().hex[:20]}",
                "stream": False,
                "workspace": "//attkr9/s",
            },
        )
        chat_lookups = _looked_up_in(recorder, 0)
        # A new project on it.
        mark = len(recorder.touches)
        project = _post(app, "/api/projects", {"name": "Share", "root": "//attkr9/s"})
        project_lookups = _looked_up_in(recorder, mark)
        # A model's workspace read of a file on it, in Auto mode.
        mark = len(recorder.touches)
        read = _execute("workspace.read_file", {"path": "//attkr9/s/x"}, {"workspace": str(workspace), "operating_mode": "auto"})
        read_decisions = len(decisions)
        read_lookups = _looked_up_in(recorder, mark)
        # The owner command centre comparing a file on it with a workspace file.
        mark = len(recorder.touches)
        compare_status, compare = _post(
            app, "/api/commands/dispatch",
            {
                "command_id": "archaeology.compare",
                "input": {"kind": "files", "a": "//attkr9/s/x.txt", "b": "b.txt", "workspace_root": "ws"},
            },
        )
        compare_lookups = _looked_up_in(recorder, mark)
        marker_starts = list(recorder.marker_popen)

    # A file:// URL naming a host, rendered by the model's tool and by the engine's own entry points.
    url = "file://attkr9/s"
    rendered = _execute("browser.render", {"url": url}, {"workspace": str(workspace), "operating_mode": "auto"})
    render_decisions = len(decisions) - read_decisions
    engine_answers = {"chrome_render": browser_render.chrome_render(url), "browser_render": browser_render.browser_render(url)}

    # /api/chat: the turn ran and answered, and the workspace was carried into it and looked up there.
    # The reply's own words depend on which models this host has, not on the path, so they are not pinned.
    assert chat_status == 200, chat
    assert (chat.get("done"), chat.get("done_reason"), (chat.get("message") or {}).get("role")) == (True, "stop", "assistant"), chat
    assert (chat.get("message") or {}).get("content"), chat
    assert (chat.get("vool_response_commit") or {}).get("status") == "answer_present", chat
    assert turns == ["hello from a posix share"], "the chat turn did not run"
    assert chat_lookups == {
        "core/standing_instructions.py",
        "core/agent_runtime/write_root_honesty.py",
        "core/agent_runtime/fast_paths_machine.py",
    }
    # /api/projects: the base's own existence check answers, after looking the folder up.
    assert project == (400, {"error": "that folder does not exist"})
    assert project_lookups == {"core/project_store.py"}
    # The executor: the call reaches the permission decision, then workspace confinement answers.
    assert (read.status, read.ok, read_decisions) == ("scope_violation", False, 1), read.response_text
    assert read.response_text == (
        "Path escapes the active workspace (ws). I can only read or change files inside the bound project"
        " folder — use a path relative to it, or open a chat bound to the other folder."
    )
    assert read_lookups == {"core/mode_permission_policy.py", "core/execution/workspace_tools.py"}
    # /api/commands/dispatch: the handler runs and reads the file, which does not exist (ENOENT), and the
    # base reports that as an internal fault: this pins the base answer, not a judgement of it. The words
    # after the errno are the recorder's, so only the errno and the path are pinned.
    assert len(compare_runs) == 1, "the compare handler did not run"
    assert (compare_status, compare.get("ok"), (compare.get("fault") or {}).get("code")) == (500, False, "internal"), compare
    summary = str(compare.get("summary") or "")
    assert summary.startswith("archaeology.compare failed: [Errno 2] ") and summary.endswith(": '//attkr9/s/x.txt'"), compare
    assert compare_lookups == {"core/project_archaeology.py"}
    assert marker_starts == [], "a process naming the share was started"
    # browser.render: the call reaches the permission decision and the browser renders the URL, as on base.
    assert (rendered.status, rendered.ok, render_decisions) == ("executed", True, 1), rendered.response_text
    assert rendered.response_text == (
        f"Rendered {url}\n- Status: ok\n- Title: {FAKE_TITLE}\n- Preview: {FAKE_TITLE} rendered off windows"
    )
    page = {
        "status": "ok",
        "final_url": url,
        "title": FAKE_TITLE,
        "text": f"{FAKE_TITLE} rendered off windows",
        "html": FAKE_PAGE,
        "links": [],
        "screenshot_path": None,
    }
    assert engine_answers == {"chrome_render": page, "browser_render": page}
    # The browser was started once per render, and its argv still carries the URL.
    argvs = _browser_argvs(browser_log)
    assert [argv[-2:] for argv in argvs] == [["--dump-dom", url]] * 3, argvs
    # And nothing anywhere said the Windows refusal.
    answers = (chat, project, read.response_text, read.user_safe_response_text, compare, rendered.response_text, engine_answers)
    assert _refusal_words(*answers) == []


def test_a_posix_double_slash_path_is_never_refused_off_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows_host
) -> None:
    # Real local folders, each named by a //-spelling of its POSIX path (`//tmp/...`). POSIX reads it as
    # the same folder; Windows would read `\\tmp\<share>`, a share on a host named after the first folder.
    workspace = _local_workspace(tmp_path, monkeypatch)
    renders = tmp_path / "renders"
    renders.mkdir()
    (renders / "render.png").write_bytes(PNG)
    monkeypatch.setenv("VOOL_FILES_DIRS", str(renders))
    acme = tmp_path / "acme"
    acme.mkdir()

    def double_slash(path: Path) -> str:
        return "//" + str(path).lstrip("/")

    render, folder, notes, compared, root = (
        double_slash(path)
        for path in (renders / "render.png", acme, workspace / "notes.txt", workspace / "a.txt", workspace)
    )
    for value in (render, folder, notes, compared, root):
        assert ntpath.splitdrive(value)[0].startswith("//"), value  # Windows reads a //host/share drive
    app = _app()
    windows_host.off()
    assert windows_path_policy._IS_WINDOWS is False

    # The Files panel serves the render: path_is_allowed accepts it, and the inline-image route returns it.
    assert generated_files.path_is_allowed(render) is True
    raw_status, _headers, raw = asgi_request(app, method="GET", path="/api/files/raw?path=" + quote(render, safe=""), headers=HOST)
    assert (raw_status, raw) == (200, PNG)
    # A project is created on the folder, under its real path, and its stored folder is found again.
    status, created = _post(app, "/api/projects", {"name": "Acme", "root": folder})
    assert status == 200, created
    assert created["project"]["root"] == os.path.realpath(acme), created
    assert project_store.project_root(created["project"]["id"]) == os.path.realpath(acme)
    # The model reads a workspace file by its //-spelling, in a workspace given by its //-spelling.
    read = _execute("workspace.read_file", {"path": notes}, {"workspace": root, "operating_mode": "auto"})
    assert (read.status, read.ok) == ("executed", True), read.response_text
    assert "local notes" in str(read.response_text or ""), read.response_text
    # The owner command centre compares two workspace files, one named and rooted by //-spellings.
    compare_input = {"kind": "files", "a": compared, "b": "b.txt", "workspace_root": root}
    status, compare = _post(app, "/api/commands/dispatch", {"command_id": "archaeology.compare", "input": compare_input})
    assert status == 200 and compare.get("ok") is True, compare
    assert compare["data"]["comparison"]["summary"].endswith(": changed"), compare["data"]["comparison"]
    assert _refusal_words(created, read.response_text, compare) == []

    # The policy's own answers, off Windows, for these paths and for a //host/share naming the marker host.
    share = "//attkr9/s/x.txt"
    for value in (render, folder, notes, compared, root, share):
        assert windows_path_policy.argument_refusal("workspace.read_file", {"path": value}) is None, value
        assert windows_path_policy.argument_refusal("machine.move_path", {"source": value, "destination": value}) is None
        assert windows_path_policy.argument_refusal("mcp.files.read_file", {"path": value}, network_only=True) is None
    for compare_fields in (compare_input, {"kind": "files", "a": share, "b": "b.txt", "workspace_root": root}):
        assert windows_path_policy.command_input_refusal("archaeology.compare", compare_fields) is None, compare_fields
    assert windows_path_policy.command_input_refusal("archaeology.search", {"workspace_root": root, "path_prefix": share}) is None

    # The same values are refused once the seam is on: the silence above is the short-circuit, not a
    # field the policy never reads or a spelling it does not recognise.
    for value in (render, folder, notes, compared, root, share):
        assert windows_path_policy.classify(value) == "unc", value
    windows_host.on()
    refused = windows_path_policy.argument_refusal("workspace.read_file", {"path": notes})
    assert refused is not None and (refused.value, refused.kind) == (notes, "unc"), refused
    refused = windows_path_policy.command_input_refusal("archaeology.compare", compare_input)
    assert refused is not None and refused.kind == "unc", refused
