"""VOOL only uses the Ollama its bundle supervisor started, never another one on the same port.

SA follow-up to the address pin (0.7.1): the supervisor counted ANY healthy answer on
127.0.0.1:11434 as its bundled Ollama (``_ensure_ollama`` -> ``_watch_child``: a healthy probe
returns at once, nothing checks who answered). If the user's own Ollama is already listening there
when VOOL starts, the bundled ``ollama serve`` fails to bind and exits, the user's Ollama keeps
answering, and VOOL's API (pinned to 127.0.0.1:11434) quietly talks to it: another model store,
no ``OLLAMA_NO_CLOUD``, whatever settings that Ollama has.

Now a healthy answer counts as the bundled Ollama only when every pid listening on the port is the
live serve the supervisor started (or the socket table shows no listener at all). Otherwise the
supervisor records a plain status, tells the user in one message, and runs no API (a running one is
stopped), so no VOOL client reaches that port. When the port no longer answers, the bundled serve
starts again as usual. A socket table that cannot be read counts as another program.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from installer.bundle import bundle_supervisor
from installer.bundle.bundle_supervisor import BundleSupervisor

FOREIGN_PID = 9999
TEXT = (
    "Another Ollama is already running on this computer and using port 11434, so VOOL cannot start its own. "
    "VOOL will not use that Ollama. Quit the other Ollama (for example from the system tray), and VOOL starts "
    "on its own."
)


def _child(pid: int, *, alive: bool = True) -> mock.Mock:
    child = mock.Mock(pid=pid)
    child.poll.return_value = None if alive else 1
    child.wait.return_value = 0
    return child


@pytest.fixture
def rig(tmp_path: Path, monkeypatch):
    root = tmp_path / "bundle"
    root.mkdir()
    home = tmp_path / "home"
    supervisor = BundleSupervisor(root, env={"VOOL_HOME": str(home), "LOCALAPPDATA": str(home)})
    supervisor._prepare()
    supervisor.log_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(bundle_supervisor, "_is_windows_platform", lambda: False)
    monkeypatch.setattr(bundle_supervisor, "_JOB_HOST", False)
    monkeypatch.setattr(supervisor, "_clear_stale_api_processes", lambda: None, raising=False)
    told: list[str] = []
    monkeypatch.setattr(supervisor, "_tell_user", lambda message: told.append(message), raising=False)
    rig = SimpleNamespace(supervisor=supervisor, told=told, listeners=set(), healthy=True)
    monkeypatch.setattr(supervisor, "_ollama_listener_pids", lambda: rig.listeners, raising=False)
    monkeypatch.setattr(supervisor, "_healthy", lambda url: rig.healthy if "11434" in url else False)
    return rig


def _ollama_pass(rig, popen: mock.Mock) -> None:
    with mock.patch.object(bundle_supervisor.subprocess, "Popen", popen):
        rig.supervisor._ensure_ollama()


def _api_pass(rig, popen: mock.Mock) -> None:
    with mock.patch.object(bundle_supervisor.subprocess, "Popen", popen):
        rig.supervisor._ensure_api()


def test_the_users_own_ollama_already_on_the_port_is_not_taken_for_vools(rig) -> None:
    rig.listeners = {FOREIGN_PID}
    popen = mock.Mock(side_effect=[_child(701), _child(702)])
    _ollama_pass(rig, popen)
    _api_pass(rig, popen)
    assert popen.call_count == 0  # no API runs, so no VOOL client talks to that Ollama
    assert rig.supervisor.ollama_conflict == TEXT
    assert rig.told == [TEXT]
    status = json.loads(rig.supervisor.model_status_path.read_text(encoding="utf-8"))
    assert (status["status"], status["detail"]) == ("ollama_port_in_use", TEXT[:240])
    rig.supervisor._write_state()
    state = json.loads(rig.supervisor.state_path.read_text(encoding="utf-8"))
    assert state["ollama_conflict"] == TEXT


def test_the_bundled_serve_that_could_not_bind_leaves_the_conflict_standing(rig) -> None:
    # Our serve was started, failed to bind (the port was taken) and exited; the other Ollama answers.
    rig.supervisor.processes["ollama"] = _child(701, alive=False)
    rig.listeners = {FOREIGN_PID}
    popen = mock.Mock(side_effect=[_child(702)])
    _ollama_pass(rig, popen)
    _api_pass(rig, popen)
    assert rig.supervisor.ollama_conflict == TEXT
    assert popen.call_count == 0


def test_a_running_api_is_stopped_when_another_program_takes_the_port(rig, monkeypatch) -> None:
    ours = _child(701)
    api = _child(702)
    rig.supervisor.processes.update({"ollama": ours, "api": api})
    rig.listeners = {701, FOREIGN_PID}
    stopped: list[str] = []
    real_stop = rig.supervisor._stop_child
    monkeypatch.setattr(rig.supervisor, "_stop_child", lambda name: (stopped.append(name), real_stop(name)))
    popen = mock.Mock()
    _ollama_pass(rig, popen)
    _api_pass(rig, popen)
    assert stopped == ["api"]
    assert "api" not in rig.supervisor.processes
    assert popen.call_count == 0


def test_the_message_is_shown_once_per_conflict(rig) -> None:
    rig.listeners = {FOREIGN_PID}
    popen = mock.Mock()
    for _ in range(3):
        _ollama_pass(rig, popen)
        _api_pass(rig, popen)
    assert rig.told == [TEXT]


def test_an_unreadable_socket_table_counts_as_another_program(rig) -> None:
    rig.supervisor.processes["ollama"] = _child(701)
    rig.listeners = None
    popen = mock.Mock()
    _ollama_pass(rig, popen)
    _api_pass(rig, popen)
    assert rig.supervisor.ollama_conflict == TEXT
    assert popen.call_count == 0


def test_vools_own_serve_on_the_port_starts_the_api_as_before(rig) -> None:
    rig.supervisor.processes["ollama"] = _child(701)
    rig.listeners = {701}
    popen = mock.Mock(side_effect=[_child(702)])
    _ollama_pass(rig, popen)
    _api_pass(rig, popen)
    assert rig.supervisor.ollama_conflict == ""
    assert popen.call_count == 1 and str(popen.call_args.args[0][1]).endswith("vool_api_server.py")
    assert rig.told == []


def test_when_the_port_frees_the_bundled_serve_starts_and_the_api_follows(rig) -> None:
    rig.listeners = {FOREIGN_PID}
    popen = mock.Mock(side_effect=[_child(701), _child(702)])
    _ollama_pass(rig, popen)
    _api_pass(rig, popen)
    assert popen.call_count == 0
    # The user quits their Ollama: nothing answers, so the bundled serve starts.
    rig.healthy = False
    rig.listeners = set()
    _ollama_pass(rig, popen)
    assert rig.supervisor.ollama_conflict == ""
    assert popen.call_count == 1 and str(popen.call_args.args[0][-1]) == "serve"
    rig.healthy, rig.listeners = True, {701}
    _ollama_pass(rig, popen)
    _api_pass(rig, popen)
    assert popen.call_count == 2 and str(popen.call_args.args[0][1]).endswith("vool_api_server.py")


def test_the_listener_table_reads_only_listening_sockets_on_the_ollama_port(tmp_path, monkeypatch) -> None:
    import psutil

    root = tmp_path / "bundle"
    root.mkdir()
    supervisor = BundleSupervisor(root, env={"VOOL_HOME": str(tmp_path / "h"), "LOCALAPPDATA": str(tmp_path / "h")})

    def conn(port: int, status: str, pid: int | None):
        return SimpleNamespace(laddr=SimpleNamespace(ip="127.0.0.1", port=port), status=status, pid=pid)

    rows = [conn(11434, "LISTEN", 701), conn(11434, "ESTABLISHED", 555), conn(11435, "LISTEN", 702),
            conn(11434, "LISTEN", None)]
    monkeypatch.setattr(psutil, "net_connections", lambda kind="tcp": rows)
    assert supervisor._ollama_listener_pids() == {701, -1}  # an unreported pid is not ours
    monkeypatch.setattr(psutil, "net_connections", lambda kind="tcp": (_ for _ in ()).throw(psutil.AccessDenied()))
    assert supervisor._ollama_listener_pids() is None


def test_the_user_is_told_only_on_windows(tmp_path, monkeypatch) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    supervisor = BundleSupervisor(root, env={"VOOL_HOME": str(tmp_path / "h"), "LOCALAPPDATA": str(tmp_path / "h")})
    started: list[str] = []
    monkeypatch.setattr(bundle_supervisor, "_is_windows_platform", lambda: False)
    monkeypatch.setattr("threading.Thread.start", lambda self: started.append(self.name))
    supervisor._tell_user(TEXT)
    assert started == []
    monkeypatch.setattr(bundle_supervisor, "_is_windows_platform", lambda: True)
    supervisor._tell_user(TEXT)
    assert started == ["vool-ollama-port-in-use"]
