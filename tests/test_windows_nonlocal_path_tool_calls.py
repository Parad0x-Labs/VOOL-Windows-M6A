"""On Windows, a tool call naming a share or device path is refused before anything reaches for it.

On Windows a stat of `\\\\attkr9\\s\\x` is an SMB connection, and an NTLM hash sent to whoever
answers. `\\attkr9\\x` and `C:attkr9\\x` are not full paths either: they land on whatever drive or
folder is current. Measured on the base (f8d2e30), every one of the calls below reached for its path
or put it to the permission decision before any answer: `workspace.write_file` with
`file_path=//attkr9/s/x` was looked up by the permission decision itself (`mode_permission_policy`),
and then, in Manual mode, the operator was asked to approve it; in Auto mode it was reserved as an A6
effect and handed to the Blackbox recorder and its handler, which looked it up again.

These drive the real executor (`execute_tool_intent`) with the policy seam
`windows_path_policy._IS_WINDOWS` on, and watch the calls themselves with
`tests/_nonlocal_touch_recorder.py`: every stat, open, listing or process start naming the marker host
`attkr9` is recorded and refused. Local fixtures are made before the seam goes on, because with it on
a POSIX absolute path such as `tmp_path` counts as root-relative. Literal sentences are asserted,
never `refusal_text`, so the same file runs against a base given only `core/windows_path_policy.py`,
with no guard wired to it.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

from core import policy_engine
from core import tool_intent_executor as executor
from core import windows_write_gate as gate
from core.hive_activity_tracker import HiveActivityTracker, HiveActivityTrackerConfig
from core.tool_argument_aliases import ARGUMENT_ALIASES
from tests._nonlocal_touch_recorder import MARKER_FORMS, MarkerForm, recorder, windows_host
from tools.browser import browser_render

STATUS = "non_local_path_refused"
#: The home folders a machine write may land in (runtime_execution_tools._SAFE_MACHINE_DIRECTORY_NAMES).
SAFE_FOLDERS = ("Desktop", "Documents", "Downloads")


def _sentence(form: MarkerForm) -> str:
    # What the user is told, with the value they gave: a share or device by the network tier, a
    # root- or drive-relative path by the full rule.
    return f"`{form.value}` " + ("is a network or device path" if form.network else "is not a full local path")


def _spy(monkeypatch: pytest.MonkeyPatch, owner: object, name: str) -> list[dict]:
    calls: list[dict] = []
    real = getattr(owner, name)

    def recording(*args, **kwargs):
        calls.append(kwargs or {"args": args})
        return real(*args, **kwargs)

    monkeypatch.setattr(owner, name, recording)
    return calls


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


def _local_machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A chat workspace, a user home and a working directory, all under tmp_path.

    The home and the working directory are moved too: machine paths are placed from the home and
    move or export paths from the working directory, so a call the base lets through can only ever
    reach a folder of this test.
    """
    from core import runtime_execution_tools

    workspace, home, elsewhere = tmp_path / "workspace", tmp_path / "home", tmp_path / "elsewhere"
    workspace.mkdir()
    elsewhere.mkdir()
    for folder in SAFE_FOLDERS:
        (home / folder).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.chdir(elsewhere)
    assert runtime_execution_tools._machine_home() == home
    return workspace, home


# Each tool that takes a filesystem path: the argument keys under test, and the local arguments that
# make the rest of the call well formed. The workspace tools are also named with the aliases models
# emit, which the dispatcher binds onto `path`.
_WORKSPACE_KEYS = ("path", "file_path", "filepath", "filename", "dir", "folder", "directory")
_TOOLS: dict[str, tuple[tuple[str, ...], dict[str, str]]] = {
    "workspace.read_file": (_WORKSPACE_KEYS, {}),
    # The workspace listing tool (there is no dispatchable `workspace.list_directory`).
    "workspace.list_files": (_WORKSPACE_KEYS, {}),
    "workspace.write_file": (_WORKSPACE_KEYS, {"content": "x"}),
    "workspace.replace_in_file": (_WORKSPACE_KEYS, {"old_text": "a", "new_text": "b"}),
    "workspace.ensure_directory": (_WORKSPACE_KEYS, {}),
    "machine.read_file": (("path",), {}),
    "machine.list_directory": (("path",), {}),
    "machine.write_file": (("path",), {"content": "x"}),
    "machine.ensure_directory": (("path",), {}),
    "machine.move_path": (("source", "destination"), {"source": "a.txt", "destination": "b.txt"}),
    "machine.disk_usage": (("drive",), {}),
    "machine.find_largest": (("drive", "path"), {}),
    "pdf.extract_text": (("path",), {}),
    "skill.install": (("path",), {}),
    "skill.list": (("workspace",), {}),
    "media.open": (("source_path",), {}),
    "media.export": (("output_path",), {"project_id": "clip"}),
    "sandbox.run_command": (("cwd",), {"command": "echo ok"}),
    "operator.inspect_disk_usage": (("target_path",), {}),
    "operator.move_path": (("source_path", "destination_path"), {"source_path": "a.txt", "destination_path": "b.txt"}),
    "code.task.identify": (("path",), {"task_id": "task-1"}),
}
_TOOL_PATHS = [pytest.param(intent, key, id=f"{intent}-{key}") for intent, (keys, _) in _TOOLS.items() for key in keys]


def _where(frames: tuple[str, ...]) -> str:
    # The innermost frame outside tests/ (conftest wraps Popen): where VOOL itself reached out.
    return next((frame for frame in reversed(frames) if not frame.startswith("tests/")), "(no VOOL frame)")


def _reached(recorder, touches_from: int, starts_from: int, *, every_start: bool = False) -> dict[str, list[str]]:
    """What one call reached for: marker touches and process starts since the counts it began with."""
    return {
        "touches": [f"{touch.op} {touch.path!r} at {_where(touch.frames)}" for touch in recorder.touches[touches_from:]],
        "process starts": [
            f"{list(spawn.argv)!r} at {_where(spawn.frames)}"
            for spawn in recorder.popen[starts_from:]
            if every_start or spawn.names_marker
        ],
    }


def _differences(observed: dict[str, dict], expected: dict[str, dict]) -> str:
    """One line per call that did not go as expected, naming only the fields that differ."""
    lines = []
    for call, fields in observed.items():
        wrong = {name: value for name, value in fields.items() if value != expected[call][name]}
        if wrong:
            lines.append(f"{call}: {wrong}")
    return "\n".join(lines)


@pytest.mark.parametrize(("intent", "key"), _TOOL_PATHS)
def test_a_tool_path_naming_a_share_is_refused_before_the_permission_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorder, windows_host, intent: str, key: str
) -> None:
    workspace, _home = _local_machine(tmp_path, monkeypatch)
    decisions = _spy(monkeypatch, executor, "decide_tool_call")
    reservations = _spy(monkeypatch, executor, "reserve_logical_effect")
    _keys, local_arguments = _TOOLS[intent]
    windows_host.on()

    # Every form in both modes: Manual is where the operator would be asked to approve the call,
    # Auto is where it would run. The forms run in one test because each case costs a full fixture
    # setup in this suite; every count below is that one call's own.
    observed: dict[str, dict] = {}
    expected: dict[str, dict] = {}
    for form in MARKER_FORMS:
        for mode in ("manual", "auto"):
            touches, starts, asked, reserved = (
                len(recorder.touches), len(recorder.popen), len(decisions), len(reservations)
            )
            execution = _execute(
                intent, {**local_arguments, key: form.value}, {"workspace": str(workspace), "operating_mode": mode}
            )
            details = dict(execution.details or {})
            observed[f"{form.id} {mode}"] = {
                "status": execution.status,
                "ok": execution.ok,
                "executed": details.get("executed"),
                # The refusal names the argument, as the model sent it or as the dispatcher binds it.
                "argument": details.get("argument") in {key, ARGUMENT_ALIASES.get(key, key)},
                "kind": details.get("kind"),
                "response says what and why": _sentence(form) in str(execution.response_text or ""),
                "user-safe response says what and why": _sentence(form) in str(execution.user_safe_response_text or ""),
                # No approval card and no A6 reservation for a call that will never run.
                "permission decisions": len(decisions) - asked,
                "A6 reservations": len(reservations) - reserved,
                **_reached(recorder, touches, starts),
            }
            expected[f"{form.id} {mode}"] = {
                "status": STATUS,
                "ok": False,
                "executed": False,
                "argument": True,
                "kind": form.kind,
                "response says what and why": True,
                "user-safe response says what and why": True,
                "permission decisions": 0,
                "A6 reservations": 0,
                "touches": [],
                "process starts": [],
            }

    assert observed == expected, _differences(observed, expected)


def _fake_browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A harmless executable pinned as the browser, the way find_browser_binary's docstring pins one.

    Playwright is made unimportable, as on a machine without it (CI installs it), so a render takes
    the Chrome fallback: the lane that starts a browser process with the URL in its argv.
    """
    binary = tmp_path / "fake-browser"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("VOOL_BROWSER_BINARY", str(binary))
    monkeypatch.setenv("PLAYWRIGHT_ENABLED", "1")
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    browser_render.reset_browser_binary_cache_for_test()
    assert browser_render.find_browser_binary() == str(binary)
    return str(binary)


_FILE_URLS = [
    pytest.param("file://attkr9/s/x.html", "is a network or device path", id="file-host"),
    pytest.param("file:////attkr9/s", "is a network or device path", id="file-four-slashes"),
    pytest.param("view-source:file://attkr9/s", "is a network or device path", id="view-source-file-host"),
    # No host: `file:///x` is `\x` on the current drive, which is not a share but is no full local path.
    pytest.param("file:///attkr9/s/x.html", "is not a full local path", id="file-no-host"),
]


@pytest.mark.parametrize(("url", "sentence"), _FILE_URLS)
def test_browser_render_of_a_file_url_naming_a_host_never_starts_a_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorder, windows_host, enable_web, url: str, sentence: str
) -> None:
    # `enable_web`: the web tools are off under pytest; the shipped default turns them on.
    workspace, _home = _local_machine(tmp_path, monkeypatch)
    _fake_browser(tmp_path, monkeypatch)
    decisions = _spy(monkeypatch, executor, "decide_tool_call")
    fetches_noted = _spy(monkeypatch, browser_render, "note_remote_fetch_attempt")
    told = f"`{url}` {sentence}"
    windows_host.on()

    # The model's call, by each name the URL argument goes by, then the engine's own entry points,
    # which the search provider and media ingestion call directly. No process may start at all.
    calls = {
        **{
            f"browser.render {key}=": lambda key=key: _execute(
                "browser.render", {key: url}, {"workspace": str(workspace), "operating_mode": "auto"}
            )
            for key in ("url", "uri", "link")
        },
        "chrome_render()": lambda: browser_render.chrome_render(url),
        "browser_render()": lambda: browser_render.browser_render(url),
    }
    observed: dict[str, dict] = {}
    for call, run in calls.items():
        touches, starts, asked, noted = len(recorder.touches), len(recorder.popen), len(decisions), len(fetches_noted)
        result = run()
        if isinstance(result, dict):  # the engine's typed result
            status, text = result.get("status"), str(result.get("error") or "")
        else:
            status, text = result.status, str(result.response_text or "")
        observed[call] = {
            "status": status,
            "says what and why": told in text,
            "permission decisions": len(decisions) - asked,
            "remote fetches noted": len(fetches_noted) - noted,
            **_reached(recorder, touches, starts, every_start=True),
        }

    refused = {
        "status": STATUS,
        "says what and why": True,
        "permission decisions": 0,
        "remote fetches noted": 0,
        "touches": [],
        "process starts": [],
    }
    expected = dict.fromkeys(calls, refused)
    assert observed == expected, _differences(observed, expected)


@pytest.fixture
def email_reading_enabled():
    """The mail read tool switched on, as `enable_web` switches the web on; no account is needed to be
    asked about a read."""
    previous = policy_engine._POLICY_CACHE
    policy = dict(policy_engine.load())
    policy["email"] = {**(policy.get("email") or {}), "read_enabled": True}
    policy_engine._POLICY_CACHE = policy
    try:
        yield
    finally:
        policy_engine._POLICY_CACHE = previous


def test_a_local_relative_path_still_reaches_the_decision_on_the_windows_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorder, windows_host, email_reading_enabled
) -> None:
    """No over-refusal: the full rule is for what a caller sends, never for the roots the server built.

    With the seam on, this host's POSIX roots (the workspace, the home) read as root-relative, which
    is why the shared resolvers apply only the network tier. A relative file is still read, a mail
    folder is still a mail folder, and the Windows write gate, called directly as the Windows gauntlet
    calls it (tests/test_machine_writes_are_checked_where_they_land.py), still places a machine path in
    the home folder it lands in, a drive-relative one included.
    """
    workspace, home = _local_machine(tmp_path, monkeypatch)
    (workspace / "notes.txt").write_text("local notes", encoding="utf-8")
    # The gauntlet's machine path (`C:Documents\x.txt` on Windows, `Documents/x.txt` here) with the
    # folder it lands in, the only one flagged, as the gauntlet flags it. A host without drives also
    # gets the drive-relative spelling, which the policy reads as drive-relative even there. Its writer
    # joins it onto the home (as a folder named `C:Documents`, which a ':'-separated
    # VOOL_WINDOWS_DISPOSABLE_WORKSPACES cannot name), so the home is flagged for it. On Windows the
    # first spelling already is the drive-relative one.
    machine_paths = [(home.drive + "Documents" + os.sep + "x.txt", home / "Documents")]
    if not home.drive:
        machine_paths.append(("C:Documents" + os.sep + "x.txt", home))
    decisions = _spy(monkeypatch, executor, "decide_tool_call")
    windows_host.on()

    read = _execute("workspace.read_file", {"path": "notes.txt"}, {"workspace": str(workspace), "operating_mode": "auto"})
    assert (read.status, read.ok) == ("executed", True), read.response_text
    assert "local notes" in str(read.response_text or "")
    assert len(decisions) == 1, "the read reached the permission decision"

    # A mail folder is not a filesystem path, so a leading slash is not refused as root-relative.
    for mailbox in ("INBOX", "/Inbox"):
        mail = _execute("email.read", {"folder": mailbox}, {"workspace": str(workspace), "operating_mode": "manual"})
        assert mail.status == "pending_approval", (mailbox, mail.status, mail.response_text)
    assert len(decisions) == 3, "each mail read reached the permission decision"

    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    for value, folder in machine_paths:
        arguments = {"path": value, "content": "x"}
        only_workspace = {gate.ENV_VAR: str(workspace)}
        with_folder = {gate.ENV_VAR: os.pathsep.join([str(workspace), str(folder)])}
        # The gate is on and places the path in that folder: refused while it is not flagged ...
        assert gate.refusal("machine.write_file", arguments, workspace, environ=only_workspace), value
        # ... and let through once it is.
        assert gate.refusal("machine.write_file", arguments, workspace, environ=with_folder) is None, value

    assert recorder.touches == [] and recorder.marker_popen == [], recorder.report()


@pytest.mark.parametrize("name", [pytest.param("//attkr9/s", id="share"), pytest.param("//attkr9/s/docs", id="share-folder")])
def test_find_folder_never_looks_up_a_share_it_is_asked_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorder, windows_host, name: str
) -> None:
    # `name` is a search phrase, not a path argument, so the door lets it through; the phrase probe
    # behind it (`_resolved_existing_directory`) is what must not look a share up as a folder.
    workspace, home = _local_machine(tmp_path, monkeypatch)
    (home / "Documents" / "invoices").mkdir()
    windows_host.on()

    result = _execute("machine.find_folder", {"name": name}, {"workspace": str(workspace), "operating_mode": "auto"})
    # Control: a local folder name is still searched for and found under the home folders.
    local = _execute("machine.find_folder", {"name": "invoices"}, {"workspace": str(workspace), "operating_mode": "auto"})

    assert recorder.touches == [] and recorder.marker_popen == [], recorder.report()
    assert result.handled, (result.status, result.response_text)
    assert (local.status, local.ok) == ("executed", True), local.response_text
    assert "invoices" in str(local.response_text or ""), local.response_text


@pytest.mark.parametrize("intent", ["machine.read_file", "machine.list_directory", "machine.write_file"])
@pytest.mark.parametrize(
    "value", [pytest.param("~///attkr9/s/x.txt", id="tilde-forward"), pytest.param("~\\\\\\attkr9\\s\\x.txt", id="tilde-backslash")]
)
def test_a_home_path_joined_onto_a_share_is_the_share(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorder, windows_host, intent: str, value: str
) -> None:
    # The machine tools place `~\rest` as the home folder joined with `rest`, and on Windows a join
    # onto a rooted `rest` is `rest` itself: `~///host/s/x` names the share.
    workspace, _home = _local_machine(tmp_path, monkeypatch)
    decisions = _spy(monkeypatch, executor, "decide_tool_call")
    windows_host.on()

    arguments = {"path": value, **({"content": "x"} if intent.endswith("write_file") else {})}
    result = _execute(intent, arguments, {"workspace": str(workspace), "operating_mode": "auto"})

    assert recorder.touches == [] and recorder.marker_popen == [], recorder.report()
    assert (result.status, (result.details or {}).get("kind")) == (STATUS, "unc"), (result.status, result.response_text)
    assert f"`{value}` is a network or device path" in str(result.response_text or ""), result.response_text
    assert decisions == [], "a permission decision was taken for a share"


def test_a_share_is_labelled_as_written_and_never_resolved(recorder, windows_host) -> None:
    from core.runtime_execution_tools import _home_relative_label

    windows_host.on()

    assert _home_relative_label(Path("//attkr9/s/docs")) == "//attkr9/s/docs"
    assert recorder.touches == [], recorder.report()


@pytest.mark.parametrize("intent", ["repo.push.request", "repo.session.open"])
def test_a_push_remote_naming_a_share_is_refused_before_it_is_looked_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorder, windows_host, intent: str
) -> None:
    # A push remote is a remote's name or a URL. One on a share is the folder the push check would look
    # up for its hooks and settings, which is already the SMB connection, so the door refuses it first.
    workspace, _home = _local_machine(tmp_path, monkeypatch)
    decisions = _spy(monkeypatch, executor, "decide_tool_call")
    local_arguments = {
        "repo.push.request": {"repo_session_id": "rs-none", "ref": "main"},
        "repo.session.open": {"objective": "push it", "provider": "github", "namespace": "o/r"},
    }[intent]
    shares = [(form.value, form.kind) for form in MARKER_FORMS if form.network]
    shares += [("file://attkr9/s/r.git", "unc"), ("file:////attkr9/s/r.git", "unc")]
    windows_host.on()

    for value, kind in shares:
        result = _execute(intent, {**local_arguments, "remote": value}, {"workspace": str(workspace), "operating_mode": "auto"})
        assert (result.status, (result.details or {}).get("kind")) == (STATUS, kind), (value, result.status, result.response_text)
        assert f"`{value}` is a network or device path" in str(result.response_text or ""), (value, result.response_text)
    assert recorder.touches == [] and recorder.marker_popen == [], recorder.report()
    assert decisions == [], "a permission decision was taken for a share"

    # A remote's name, a hosted URL and a local drive path are not refused here.
    for value in ("origin", "https://github.com/o/r.git", "git@github.com:o/r.git", "C:/repos/r.git"):
        result = _execute(intent, {**local_arguments, "remote": value}, {"workspace": str(workspace), "operating_mode": "auto"})
        assert result.status != STATUS, (value, result.response_text)


def test_a_push_remote_the_repository_names_on_a_share_is_refused_before_it_is_looked_up(
    tmp_path: Path, recorder, windows_host
) -> None:
    # The repository's own config can name a remote on a share: repository contents the tool-call door
    # never sees. The push check refuses that destination as written, before it would look it up for
    # the hooks and settings of a repository there.
    import subprocess

    from core.repoops.gitops import GitRunner

    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    remotes = {"forward": "//attkr9/s/r.git", "backslash": "\\\\attkr9\\s\\r.git", "file-url": "file:////attkr9/s/r.git"}
    # Written straight into the config file: a process start naming the share is refused by the
    # recorder. A config value escapes its backslashes.
    with (repo / ".git" / "config").open("a", encoding="utf-8") as config:
        config.writelines(f'[remote "{name}"]\n\turl = {url.replace(chr(92), chr(92) * 2)}\n' for name, url in remotes.items())
    windows_host.on()

    refusals = {name: GitRunner(repo).push_destination_refusal(name) for name in remotes}

    assert recorder.touches == [] and recorder.marker_popen == [], recorder.report()
    for name, url in remotes.items():
        assert f"`{url}` is a network or device path" in refusals[name], (name, refusals[name])
