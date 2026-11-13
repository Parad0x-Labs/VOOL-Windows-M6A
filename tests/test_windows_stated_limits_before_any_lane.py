"""On Windows, a message that asks for a stated 0.7.1 limit is refused in plain words before any lane.

Clean-VM round 1 (Windows 11, installed build 828b8bda, qwen3:8b). These already got the plain
refusal with no model call: "git status", npm, node, ruff, calc.exe, cmd, powershell,
"python -m ruff" and a `\\\\server\\share` path. These did not:

1. "commit this" was answered by the model ("Nothing is running right now..."), not with the git
   sentence; so were "push to GitHub", "make a commit" and the other git asks.
2. A bare "pip install requests" (and "pip3 install x", "install requests with pip",
   "py -m pip install x") got the model's offer to help.
3. "run python -m pip install x" and "run python -X importtime -c 0" went into a builder step.
4. "run <venv>\\Scripts\\python.exe t.py" got a model chat; it must name the allowed interpreter.
5. "write hi to \\\\?\\C:\\x.txt" got a model reply: the message was split at its "?" ahead of the
   front door, so neither half was a device path.

Each served `/api/chat` turn here runs with the Windows decision switched on through the modules'
own seams (`job_runner._is_windows_platform`, `network_guard._IS_WINDOWS`, `safe_git._REFUSE_GIT`,
`windows_write_gate._IS_WINDOWS`, `windows_path_policy._IS_WINDOWS`), never through `os.name`, as
the other Windows capability-test files do. A spy records every provider call, every lane the turn
enters after the secret intake (follow-up, conductor, demand, live data, planner, front door, the
model lane), every permission decision, approval request and sandbox job: a refused turn has none.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest

from core import mode_permission_policy, safe_git, windows_path_policy, windows_write_gate
from core import runtime_execution_tools as rte
from core import tool_intent_executor as executor
from sandbox import job_runner, network_guard

GIT_ASKS = [
    "commit this",
    "commit my changes",
    "make a commit",
    "push to GitHub",
    "push",
    "pull",
    "pull from origin",
    "create a branch called feature",
    "merge main into this branch",
    "stash my changes",
    "show me the diff",
    "show the git log",
    "clone https://github.com/a/b",
    "checkout main",
    "please commit this.",
    "can you push my changes to github?",
    "git status",
]
#: The message, and the program the plain refusal names.
PIP_ASKS = {
    "pip install requests": "pip",
    "pip3 install x": "pip3",
    "install requests with pip": "pip",
    "use pip to install requests": "pip",
}
#: The message, and the command line the run lane and a tool call are asked about.
PYTHON_FORMS = {
    "py -m pip install x": "py -m pip install x",
    "run python -m pip install x": "python -m pip install x",
    "run python -X importtime -c 0": "python -X importtime -c 0",
}
VENV_PYTHON = "C:\\work\\venv\\Scripts\\python.exe"
#: The message, and the device path it names, as typed.
DEVICE_WRITES = {
    "write hi to \\\\?\\C:\\x.txt": "\\\\?\\C:\\x.txt",
    "write hi to \\\\.\\C:\\x.txt": "\\\\.\\C:\\x.txt",
    "write hi to //?/C:/x.txt": "//?/C:/x.txt",
    "write hi to \\\\?\\D:\\x.txt": "\\\\?\\D:\\x.txt",
    "write hi to \\\\.\\E:\\notes.txt": "\\\\.\\E:\\notes.txt",
    "write hi to //./D:/x.txt": "//./D:/x.txt",
    "can you write hi to \\\\?\\C:\\x.txt?": "\\\\?\\C:\\x.txt",
}
#: R1-10: asks to add, start or connect an MCP server, or to install, enable or run a plugin or an
#: add-on with runnable parts. Each gets the plugin and MCP refusal (core.plugin_executor).
PLUGIN_MCP_ASKS = [
    "add an MCP server for my notes at C:\\tools\\server.py",
    "start the MCP server",
    "connect to the github MCP server",
    "run the MCP server",
    "set up the filesystem MCP server",
    "install the weather plugin",
    "enable the database plugin",
    "run the vool-database plugin",
    "enable the add-on that runs scripts",
]
#: Messages that mention a limit's word but ask for none of them.
NOT_LIMITS = [
    "what is MCP?",
    "explain plugins",
    "what is git?",
    "explain pip",
    "commit to memory that my dog is Rex",
    "write a python script that reads git_log.txt and counts the lines",
    "explain git rebase",
    "merge these two lists",
    "check out this article",
    "pull qwen3:8b",
    "make a commit message generator in python",
    "pip is slow, why?",
    "python is fun",
]

#: The lanes a served turn may enter after the secret intake, in the order the turn reaches them.
LANES = (
    "_maybe_answer_attempt_followup_turn",
    "_maybe_answer_conductor_turn",
    "_maybe_answer_demand_owned_turn",
    "_maybe_answer_live_data_turn",
    "_maybe_answer_planned_turn",
    "_handle_turn_frontdoor",
    "_prepare_turn_task_bundle",
    "_execute_grounded_turn",
)


def _plain(program: str) -> str:
    return job_runner.WINDOWS_PROGRAM_REFUSED_TEXT.format(program=program)


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(job_runner, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(network_guard, "_IS_WINDOWS", True)
    monkeypatch.setattr(safe_git, "_REFUSE_GIT", True)
    monkeypatch.setattr(windows_write_gate, "_IS_WINDOWS", True)
    monkeypatch.setattr(windows_path_policy, "_IS_WINDOWS", True)


@pytest.fixture
def off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(job_runner, "_is_windows_platform", lambda: False)
    monkeypatch.setattr(network_guard, "_IS_WINDOWS", False)
    monkeypatch.setattr(safe_git, "_REFUSE_GIT", False)
    monkeypatch.setattr(windows_write_gate, "_IS_WINDOWS", False)
    monkeypatch.setattr(windows_path_policy, "_IS_WINDOWS", False)
    from core import plugin_executor

    monkeypatch.setattr(plugin_executor, "_WINDOWS_REFUSES_PLUGINS_AND_MCP", False)


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Records provider calls, lanes, permission decisions, approval requests and sandbox jobs."""
    from apps.vool_agent import VoolAgent
    from core import memory_first_router, turn_model_call_ledger

    seen: dict[str, list] = {"model_calls": [], "lanes": [], "decisions": [], "approvals": [], "jobs": []}

    def model_call(*_args, **_kwargs):
        seen["model_calls"].append("_invoke_manifest")
        raise AssertionError("a model was called for a turn the stated limits answer")

    monkeypatch.setattr(memory_first_router.MemoryFirstRouter, "_invoke_manifest", model_call)
    for module in (memory_first_router, turn_model_call_ledger):
        real_record = module.record_provider_call

        def record(*args, _real=real_record, **kwargs):
            seen["model_calls"].append("record_provider_call")
            return _real(*args, **kwargs)

        monkeypatch.setattr(module, "record_provider_call", record)
    for name in LANES:
        real = getattr(VoolAgent, name)

        def lane(self, *args, _name=name, _real=real, **kwargs):
            seen["lanes"].append(_name)
            return _real(self, *args, **kwargs)

        monkeypatch.setattr(VoolAgent, name, lane)
    real_decide = executor.decide_tool_call
    real_request = mode_permission_policy._new_approval_request

    def decide(**kwargs):
        seen["decisions"].append(kwargs.get("intent"))
        return real_decide(**kwargs)

    def new_request(**kwargs):
        seen["approvals"].append(kwargs.get("tool_name") or kwargs.get("intent"))
        return real_request(**kwargs)

    def run(self, argv, **_kwargs):
        seen["jobs"].append(list(argv))
        raise AssertionError("a sandbox job started")

    monkeypatch.setattr(executor, "decide_tool_call", decide)
    monkeypatch.setattr(mode_permission_policy, "_new_approval_request", new_request)
    monkeypatch.setattr(job_runner.JobRunner, "run", run)
    return seen


@pytest.fixture
def chat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """One served /api/chat turn as the window sends it, with the test backend (no model)."""
    from apps import vool_api_server
    from apps.vool_agent import VoolAgent
    from core.web.api.runtime import RuntimeServices
    from tests.asgi_harness import asgi_request

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("VOOL_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    (tmp_path / "workspace").mkdir()
    agent = VoolAgent(backend_name="test-backend", device="stated-limits", persona_id="default")
    monkeypatch.setattr(vool_api_server, "_agent", agent)
    app = vool_api_server.create_app(RuntimeServices(display_name="VOOL", agent=agent))
    headers = {"Content-Type": "application/json", "Host": "127.0.0.1", "Origin": "http://127.0.0.1:11435"}

    def send(text: str) -> str:
        body = {"messages": [{"role": "user", "content": text}], "session_id": f"openclaw:{uuid.uuid4().hex[:20]}", "stream": False}
        status, _headers, raw = asgi_request(app, method="POST", path="/api/chat", headers=headers, body=json.dumps(body).encode("utf-8"))
        assert status == 200
        payload = json.loads(raw.decode("utf-8") or "{}")
        return str((payload.get("message") or {}).get("content") or "")

    return send


def _nothing_ran(seen: dict[str, list]) -> None:
    assert seen == {"model_calls": [], "lanes": [], "decisions": [], "approvals": [], "jobs": []}, seen


def _tool_call_refusal(command: str) -> str:
    refusal = rte.windows_write_refusal_before_decision("sandbox.run_command", {"command": command}, source_context=None)
    assert refusal is not None, command
    return str(refusal.response_text or "")


# --------------------------------------------------------------------------------------
# Served turns: refused before any lane, model call or approval
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", GIT_ASKS)
def test_a_git_ask_gets_the_git_sentence_before_any_lane(windows, spy, chat, text: str) -> None:
    reply = chat(text)

    assert reply == safe_git.GIT_REFUSED_TEXT
    _nothing_ran(spy)


@pytest.mark.parametrize("text", sorted(PIP_ASKS))
def test_a_bare_pip_ask_gets_the_plain_refusal_before_any_lane(windows, spy, chat, text: str) -> None:
    reply = chat(text)

    assert reply == _plain(PIP_ASKS[text])
    _nothing_ran(spy)


@pytest.mark.parametrize("text", sorted(PYTHON_FORMS))
def test_a_python_form_phase1_refuses_gets_the_run_lanes_refusal_before_any_lane_builder_included(
    windows, spy, chat, text: str
) -> None:
    expected = _tool_call_refusal(PYTHON_FORMS[text])

    reply = chat(text)

    assert reply == expected
    assert "Python and pytest" in reply and "It was not run." in reply
    _nothing_ran(spy)


def test_another_python_interpreter_is_refused_naming_the_allowed_one(windows, spy, chat) -> None:
    interpreter = str(Path(sys.executable).resolve())

    reply = chat(f"run {VENV_PYTHON} t.py")

    assert reply == job_runner.WINDOWS_OTHER_PYTHON_REFUSED_TEXT.format(interpreter=interpreter, program=VENV_PYTHON)
    assert interpreter in reply and VENV_PYTHON in reply and "Nothing was run." in reply
    _nothing_ran(spy)


@pytest.mark.parametrize("text", sorted(DEVICE_WRITES))
def test_a_device_path_write_gets_the_device_refusal_before_any_lane(windows, spy, chat, text: str) -> None:
    path = DEVICE_WRITES[text]

    reply = chat(text)

    assert reply == windows_path_policy.refusal_text(path, "device", nothing="written")
    _nothing_ran(spy)


@pytest.mark.parametrize("text", PLUGIN_MCP_ASKS)
def test_a_plugin_or_mcp_ask_gets_the_plugin_refusal_before_any_lane(windows, spy, chat, text: str) -> None:
    from core.plugin_executor import WINDOWS_PLUGIN_MCP_REFUSAL

    reply = chat(text)

    assert reply == WINDOWS_PLUGIN_MCP_REFUSAL
    _nothing_ran(spy)


def test_the_plugin_reader_reads_plugin_and_mcp_asks_and_only_them() -> None:
    from core.agent_runtime.windows_stated_limits import asks_for_plugin_or_mcp

    assert [text for text in PLUGIN_MCP_ASKS if not asks_for_plugin_or_mcp(text)] == []
    other = [
        "what is an MCP server?",
        "explain how MCP servers work",
        "how do I install a plugin?",
        "write a plugin loader in python",
        "create an MCP client in python",
        "install the weather add-on",
        "commit to memory that MCP means model context protocol",
    ]
    assert [text for text in NOT_LIMITS + other if asks_for_plugin_or_mcp(text)] == []


def test_a_share_write_still_gets_the_network_refusal(windows, spy, chat) -> None:
    reply = chat("write hi to \\\\server\\share\\x.txt")

    assert reply == windows_path_policy.refusal_text("\\\\server\\share\\x.txt", "unc", nothing="written")
    _nothing_ran(spy)


@pytest.mark.parametrize("text", NOT_LIMITS[:6])
def test_a_message_that_only_mentions_a_limit_is_not_refused(windows, spy, chat, text: str) -> None:
    reply = chat(text)

    assert reply not in {safe_git.GIT_REFUSED_TEXT, _plain("pip"), _plain("git")}
    assert "VOOL 0.7.1" not in reply
    assert "network or device path" not in reply
    assert spy["lanes"], "the turn went on to the lanes that own it"


@pytest.mark.parametrize("text", ["commit this", "pip install requests", "write hi to \\\\?\\C:\\x.txt", "start the MCP server"])
def test_off_windows_a_served_turn_is_not_refused(off_windows, spy, chat, text: str) -> None:
    reply = chat(text)

    assert reply != safe_git.GIT_REFUSED_TEXT
    assert "VOOL 0.7.1" not in reply
    assert "network or device path" not in reply
    assert spy["lanes"]


# --------------------------------------------------------------------------------------
# The readers and the owners they ask
# --------------------------------------------------------------------------------------


def test_the_git_reader_reads_git_asks_and_only_them() -> None:
    from core.agent_runtime.windows_stated_limits import asks_for_git

    more = [
        "commit",
        "commit all my changes and push",
        "git commit -am 'x'",
        "create a new branch",
        "switch to main",
        "delete the feature branch",
        "list branches",
        "what branch am I on?",
        "rebase onto main",
        "undo the last commit",
        "check out main",
        "clone the repo",
        "what's the git status of this folder",
        "merge feature/login into main",
    ]
    assert [text for text in GIT_ASKS + more if not asks_for_git(text)] == []
    other = [
        "how do I commit?",
        "merge a.csv into b.csv",
        "pull the latest model",
        "push the button",
        "show me the diff between a.py and b.py",
        "create a branch_merge.py script",
        "commit to the plan",
        "what does git stash do?",
        "create gitignore.py",
        "show me the log",
        "run git_helper.py",
        "remember to push to github tomorrow",
        "commit to memory: my repo is on github",
        "write a function called git_push in utils.py",
    ]
    assert [text for text in NOT_LIMITS + other if asks_for_git(text)] == []


def test_a_bare_installer_or_tool_command_line_is_read_as_one() -> None:
    from core.agent_runtime.windows_program_request import requested_command

    assert requested_command("pip install requests") == "pip install requests"
    assert requested_command("pip3 install x") == "pip3 install x"
    assert requested_command("install requests with pip") == "pip install requests"
    assert requested_command("use pip to install requests") == "pip install requests"
    assert requested_command("py -m pip install x") == "py -m pip install x"
    assert requested_command("npm install") == "npm install"
    for text in ("explain pip", "pip is slow, why?", "python is fun", "python hello.py", "install requests"):
        assert requested_command(text) == "", text


def test_the_early_check_asks_the_owners_and_allows_what_they_allow(windows) -> None:
    from core.agent_runtime.windows_stated_limits import windows_limit_refusal

    assert windows_limit_refusal("commit this") == (safe_git.GIT_REFUSED_TEXT, "windows_git_refused")
    assert windows_limit_refusal("pip install requests") == (_plain("pip"), "windows_program_refused")
    assert windows_limit_refusal("write hi to \\\\?\\C:\\x.txt")[1] == "non_local_path_refused"
    for text in ("run python hello.py", "run pytest -q", "python -c pass", "remember my backups are on \\\\nas\\s", *NOT_LIMITS):
        assert windows_limit_refusal(text) == ("", ""), text


def test_off_windows_the_early_check_refuses_nothing(off_windows) -> None:
    from core.agent_runtime.windows_stated_limits import windows_limit_refusal

    for text in (*GIT_ASKS, *PIP_ASKS, *PYTHON_FORMS, *DEVICE_WRITES, *PLUGIN_MCP_ASKS, f"run {VENV_PYTHON} t.py"):
        assert windows_limit_refusal(text) == ("", ""), text


def test_a_tool_call_for_another_interpreter_gets_the_same_refusal_before_its_decision(windows) -> None:
    interpreter = str(Path(sys.executable).resolve())
    expected = job_runner.WINDOWS_OTHER_PYTHON_REFUSED_TEXT.format(interpreter=interpreter, program=VENV_PYTHON)

    assert job_runner.windows_program_refusal([VENV_PYTHON, "t.py"]) == expected
    assert _tool_call_refusal(f"{VENV_PYTHON} t.py") == expected
    assert job_runner.windows_program_refusal(["calc.exe"]) == _plain("calc.exe")
    assert job_runner.windows_program_refusal([sys.executable, "-c", "pass"]) is None


def test_the_early_check_looks_nothing_up_on_disk(windows, monkeypatch: pytest.MonkeyPatch) -> None:
    """It runs ahead of lanes that refuse a path before it is looked up (a heartbeat poll), so it
    must find a share or device by its spelling alone and never extend a local path by looking."""
    import os

    from core.agent_runtime.windows_stated_limits import windows_limit_refusal

    looked: list[str] = []

    def no_lookup(name):
        def record(path, *args, **kwargs):
            looked.append(f"{name} {path}")
            raise OSError(f"{name} {path!r} was looked up")

        return record

    texts = [
        "Read C://attkr9/s/HEARTBEAT.md. If nothing needs attention, reply HEARTBEAT_OK.",
        "write hi to C:\\Users\\me\\My Folder\\a b.txt",
        "write hi to \\\\?\\C:\\My Folder\\x.txt",
    ]
    with monkeypatch.context() as patched:
        for name in ("exists", "isdir", "isfile"):
            patched.setattr(os.path, name, no_lookup(f"os.path.{name}"))
        patched.setattr(os, "scandir", no_lookup("os.scandir"))
        answers = [windows_limit_refusal(text) for text in texts]

    assert looked == []
    assert answers[0] == ("", "") and answers[1] == ("", "")
    assert answers[2][1] == "non_local_path_refused"
