"""The bundled Ollama's ownership check fails closed, reruns on every change, and covers every address.

SA conditions on the own-serve car:

1. An unknown answer is not ours. A socket table that cannot be read (psutil AccessDenied, or any
   other exception), a listener whose pid is not reported (None or 0), and a healthy answer with no
   listener seen at all each mean ``ollama_port_in_use`` and no API. The only answer that is ours is
   the live serve this supervisor started, listed as the one and only listener on port 11434.
2. Re-verify on every change. Every pass that finds the port answering, and every API start (a
   restart, a recovery), reruns the full check before the API child starts. After a conflict the
   API waits for a passing check even while the port is quiet.
3. The check covers every listener on port 11434, IPv4 and IPv6 (127.0.0.1, 0.0.0.0, ::1, ::), and
   the Windows script installer dials the address literal 127.0.0.1, never ``localhost``.

These run the real ``_ollama_listener_pids`` against a fake psutil socket table; nothing listens.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import psutil
import pytest

from installer.bundle import bundle_supervisor
from installer.bundle.bundle_supervisor import BundleSupervisor

REPO = Path(__file__).resolve().parents[1]
OURS = 701
FOREIGN = 9999


def _child(pid: int, *, alive: bool = True) -> mock.Mock:
    child = mock.Mock(pid=pid)
    child.poll.return_value = None if alive else 1
    child.wait.return_value = 0
    return child


def _listen(pid, ip: str = "127.0.0.1", port: int = 11434, status: str = "LISTEN") -> SimpleNamespace:
    return SimpleNamespace(laddr=SimpleNamespace(ip=ip, port=port), raddr=(), status=status, pid=pid)


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
    monkeypatch.setattr(supervisor, "_clear_stale_api_processes", lambda: None)
    told: list[str] = []
    monkeypatch.setattr(supervisor, "_tell_user", lambda message: told.append(message))
    rig = SimpleNamespace(supervisor=supervisor, told=told, rows=[], raises=None, healthy=True, started=[])

    def net_connections(kind="inet"):
        if rig.raises is not None:
            raise rig.raises
        return list(rig.rows)

    monkeypatch.setattr(psutil, "net_connections", net_connections)
    monkeypatch.setattr(supervisor, "_healthy", lambda url: rig.healthy if ":11434/" in url else False)

    def popen(argv, **kwargs):
        child = _child(OURS + 1 + len(rig.started))
        rig.started.append(("api" if str(argv[1]).endswith("vool_api_server.py") else "ollama", child))
        return child

    rig.popen = popen
    return rig


def _pass(rig) -> None:
    """One supervisor pass for Ollama and the API, as run() makes it."""
    with mock.patch.object(bundle_supervisor.subprocess, "Popen", side_effect=rig.popen):
        rig.supervisor._reap_dead("ollama")
        rig.supervisor._reap_dead("api")
        rig.supervisor._ensure_ollama()
        rig.supervisor._ensure_api()


def _api_starts(rig) -> int:
    return sum(1 for name, _ in rig.started if name == "api")


def _ours_running(rig) -> None:
    rig.supervisor.processes["ollama"] = _child(OURS)


# 1. An unknown answer is not ours.


@pytest.mark.parametrize(
    "error",
    [psutil.AccessDenied(), psutil.Error("table"), PermissionError("denied"), OSError("io"), RuntimeError("any")],
    ids=["access-denied", "psutil-error", "permission-error", "os-error", "any-exception"],
)
def test_a_socket_table_that_cannot_be_listed_is_not_ours(rig, error) -> None:
    _ours_running(rig)
    rig.raises = error
    _pass(rig)
    assert rig.supervisor.ollama_conflict
    assert _api_starts(rig) == 0
    status = (rig.supervisor.model_status_path.read_text(encoding="utf-8"))
    assert "ollama_port_in_use" in status


@pytest.mark.parametrize("pid", [None, 0], ids=["pid-none", "pid-zero"])
def test_a_listener_whose_pid_is_not_reported_is_not_ours(rig, pid) -> None:
    _ours_running(rig)
    rig.rows = [_listen(pid)]
    _pass(rig)
    assert rig.supervisor.ollama_conflict
    assert _api_starts(rig) == 0


@pytest.mark.parametrize("pid", [None, 0], ids=["pid-none", "pid-zero"])
def test_an_unreported_pid_beside_our_own_serve_is_not_ours(rig, pid) -> None:
    _ours_running(rig)
    rig.rows = [_listen(OURS), _listen(pid, "::1")]
    _pass(rig)
    assert rig.supervisor.ollama_conflict
    assert _api_starts(rig) == 0


def test_a_healthy_answer_with_no_listener_seen_is_not_ours(rig) -> None:
    _ours_running(rig)  # our serve is alive, but the table lists no listener for the answer
    rig.rows = [_listen(OURS, port=11435), _listen(OURS, status="ESTABLISHED")]
    _pass(rig)
    assert rig.supervisor.ollama_conflict
    assert _api_starts(rig) == 0


def test_a_healthy_answer_with_no_serve_of_ours_and_no_listener_is_not_ours(rig) -> None:
    rig.rows = []
    _pass(rig)
    assert rig.supervisor.ollama_conflict
    assert _api_starts(rig) == 0
    assert rig.started == []  # the port answers, so no serve of ours is started over it


def test_our_live_serve_listed_as_the_only_listener_is_ours(rig) -> None:
    _ours_running(rig)
    rig.rows = [_listen(OURS), _listen(OURS, "::1"), _listen(555, port=11435)]
    _pass(rig)
    assert rig.supervisor.ollama_conflict == ""
    assert _api_starts(rig) == 1
    assert rig.told == []


def test_our_serve_listed_after_it_exited_is_not_ours(rig) -> None:
    rig.supervisor.processes["ollama"] = _child(OURS, alive=False)
    rig.rows = [_listen(OURS)]
    with mock.patch.object(bundle_supervisor.subprocess, "Popen", side_effect=rig.popen):
        rig.supervisor._ensure_ollama()
        rig.supervisor._ensure_api()
    assert rig.supervisor.ollama_conflict
    assert _api_starts(rig) == 0


# 2. Re-verify on every change.


def test_our_serve_exits_and_a_foreign_pid_takes_the_port_so_vool_stays_down(rig) -> None:
    _ours_running(rig)
    rig.rows = [_listen(OURS)]
    _pass(rig)
    assert _api_starts(rig) == 1 and "api" in rig.supervisor.processes
    # Our serve exits; the user's Ollama binds 11434 before VOOL starts another.
    rig.supervisor.processes["ollama"].poll.return_value = 1
    rig.rows = [_listen(FOREIGN)]
    for _ in range(5):
        _pass(rig)
    assert "api" not in rig.supervisor.processes  # the running API was stopped
    assert _api_starts(rig) == 1  # and none started again
    assert [name for name, _ in rig.started].count("ollama") == 0  # nothing started over the answering port
    assert rig.supervisor.ollama_conflict
    assert rig.supervisor.health["ollama"]["owner"] == "another_program"
    assert "ollama_port_in_use" in rig.supervisor.model_status_path.read_text(encoding="utf-8")
    assert len(rig.told) == 1
    rig.supervisor._write_state()
    assert '"ollama_conflict": "Another Ollama' in rig.supervisor.state_path.read_text(encoding="utf-8")


def test_an_api_restart_reruns_the_full_check_before_the_child_starts(rig, monkeypatch) -> None:
    _ours_running(rig)
    rig.rows = [_listen(OURS)]
    _pass(rig)
    assert _api_starts(rig) == 1
    # The API exits. Before its restart, a foreign listener joins the port on ::1.
    rig.supervisor.processes["api"].poll.return_value = 1
    rig.supervisor.next_attempt["api"] = 0.0
    rig.rows = [_listen(OURS), _listen(FOREIGN, "::1")]
    checks: list[str] = []
    real = rig.supervisor._ollama_is_ours
    monkeypatch.setattr(rig.supervisor, "_ollama_is_ours", lambda: (checks.append("check"), real())[1])
    with mock.patch.object(bundle_supervisor.subprocess, "Popen", side_effect=rig.popen):
        rig.supervisor._reap_dead("api")
        rig.supervisor.next_attempt["api"] = 0.0
        rig.supervisor._ensure_api()  # the API start alone, without the Ollama pass before it
    assert checks == ["check"]
    assert _api_starts(rig) == 1
    assert rig.supervisor.ollama_conflict


def test_an_api_restart_on_our_own_serve_still_starts(rig) -> None:
    _ours_running(rig)
    rig.rows = [_listen(OURS)]
    _pass(rig)
    rig.supervisor.processes["api"].poll.return_value = 1
    with mock.patch.object(bundle_supervisor.subprocess, "Popen", side_effect=rig.popen):
        rig.supervisor._reap_dead("api")
        rig.supervisor.next_attempt["api"] = 0.0
        rig.supervisor._ensure_api()
    assert _api_starts(rig) == 2
    assert rig.supervisor.ollama_conflict == ""


def test_after_a_conflict_a_quiet_port_starts_no_api_until_our_serve_passes_the_check(rig) -> None:
    rig.rows = [_listen(FOREIGN)]
    _pass(rig)
    assert rig.supervisor.ollama_conflict and _api_starts(rig) == 0
    # The user quits their Ollama: nothing answers. Our serve starts, but no API yet.
    rig.healthy, rig.rows = False, []
    _pass(rig)
    assert [name for name, _ in rig.started] == ["ollama"]
    assert rig.supervisor.ollama_needs_verify
    for _ in range(3):  # our serve is still starting: the port stays quiet
        _pass(rig)
    assert _api_starts(rig) == 0
    # Our serve answers and is the only listener: now the API starts.
    serve = rig.started[0][1]
    rig.healthy, rig.rows = True, [_listen(serve.pid)]
    _pass(rig)
    assert _api_starts(rig) == 1
    assert rig.supervisor.ollama_conflict == "" and not rig.supervisor.ollama_needs_verify


def test_after_a_conflict_the_port_answering_again_by_another_program_starts_no_api(rig) -> None:
    rig.rows = [_listen(FOREIGN)]
    _pass(rig)
    rig.healthy, rig.rows = False, []
    _pass(rig)  # quiet: our serve starts
    serve = rig.started[0][1]
    serve.poll.return_value = 1  # it fails to bind: another Ollama came back first
    rig.healthy, rig.rows = True, [_listen(FOREIGN + 4)]
    for _ in range(3):
        _pass(rig)
    assert _api_starts(rig) == 0
    assert rig.supervisor.ollama_conflict


# 3. Every listener on the port, IPv4 and IPv6.


@pytest.mark.parametrize("ip", ["::1", "::", "0.0.0.0", "127.0.0.1"])
def test_a_foreign_listener_on_any_address_alone_is_not_ours(rig, ip) -> None:
    _ours_running(rig)
    rig.rows = [_listen(FOREIGN, ip)]
    _pass(rig)
    assert rig.supervisor.ollama_conflict
    assert _api_starts(rig) == 0


def test_a_foreign_listener_on_ipv6_loopback_only_beside_our_serve_is_not_ours(rig) -> None:
    _ours_running(rig)  # ours holds 127.0.0.1:11434; the user's Ollama holds only [::1]:11434
    rig.rows = [_listen(OURS, "127.0.0.1"), _listen(FOREIGN, "::1")]
    _pass(rig)
    assert rig.supervisor.ollama_conflict
    assert _api_starts(rig) == 0


def test_a_foreign_listener_on_ipv6_loopback_only_with_no_serve_of_ours_is_not_ours(rig) -> None:
    rig.rows = [_listen(FOREIGN, "::1")]
    _pass(rig)
    assert rig.supervisor.ollama_conflict
    assert _api_starts(rig) == 0


def test_the_windows_script_installer_dials_the_address_literal() -> None:
    text = (REPO / "installer" / "install_vool.bat").read_text(encoding="utf-8", errors="replace")
    assert "localhost:11434" not in text
    assert "http://127.0.0.1:11434" in text


def test_the_windows_bundle_client_paths_never_dial_localhost_for_ollama() -> None:
    paths = [
        REPO / "installer" / "install_vool.bat",
        REPO / "installer" / "bundle" / "bundle_supervisor.py",
        REPO / "installer" / "bundle" / "native_runtime_supervisor.py",
        REPO / "installer" / "bundle" / "build_bundle.ps1",
        REPO / "core" / "ollama_endpoint.py",
    ]
    for path in paths:
        assert "localhost:11434" not in path.read_text(encoding="utf-8", errors="replace"), path
