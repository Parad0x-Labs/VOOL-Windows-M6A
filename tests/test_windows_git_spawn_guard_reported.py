"""The git spawn guard's real state is reported, and an unarmed guard fails the Windows install smoke.

/api/runtime/capabilities carries `git_spawn_guard`, read from the live audit hook (a private audit
event the hook answers), never from a constant. tools/installed_slices.py, with
--require-git-spawn-guard (the Windows install smoke passes it), fails unless that field says armed.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from core import safe_git

ROOT = Path(__file__).resolve().parents[1]
COMMIT = "a" * 40


def test_the_state_is_read_from_the_hook_not_from_a_flag(monkeypatch) -> None:
    monkeypatch.setattr(safe_git, "_REFUSE_GIT", True)
    monkeypatch.setattr(safe_git, "_GUARD_INSTALLED", True)  # a flag that lies must not read as armed
    if not safe_git.spawn_guard_live():
        assert safe_git.spawn_guard_state()["armed"] is False
        assert safe_git.spawn_guard_state()["status"] == "unarmed"
    monkeypatch.setattr(safe_git, "_GUARD_INSTALLED", False)
    monkeypatch.setattr(safe_git, "_GUARD_ARMED", safe_git._GUARD_ARMED)  # disarmed again after the row
    safe_git.install_spawn_guard()
    state = safe_git.spawn_guard_state()
    assert state["hook_live"] is True and state["armed"] is True and state["status"] == "armed"


def test_a_windows_process_without_the_hook_reports_unarmed(monkeypatch) -> None:
    monkeypatch.setattr(safe_git, "_REFUSE_GIT", True)
    monkeypatch.setattr(safe_git, "spawn_guard_live", lambda: False)
    assert safe_git.spawn_guard_state() == {
        "status": "unarmed", "armed": False, "hook_live": False, "refuses_git": True,
        "inherited_marker": safe_git.spawn_guard_state()["inherited_marker"],
    }


def test_off_windows_it_reports_not_needed(monkeypatch) -> None:
    monkeypatch.setattr(safe_git, "_REFUSE_GIT", False)
    state = safe_git.spawn_guard_state()
    assert state["status"] == "not_needed" and state["armed"] is False


def test_the_capabilities_snapshot_carries_the_live_state(monkeypatch) -> None:
    from core import runtime_capabilities

    monkeypatch.setattr(safe_git, "_REFUSE_GIT", True)
    monkeypatch.setattr(safe_git, "spawn_guard_live", lambda: False)
    assert runtime_capabilities._git_spawn_guard_capability()["status"] == "unarmed"
    monkeypatch.setattr(safe_git, "spawn_guard_live", lambda: True)
    assert runtime_capabilities._git_spawn_guard_capability()["status"] == "armed"

    def broken():
        raise RuntimeError("unreadable")

    monkeypatch.setattr(safe_git, "spawn_guard_state", broken)
    assert runtime_capabilities._git_spawn_guard_capability()["armed"] is False


def test_the_served_snapshot_has_the_field() -> None:
    from core.runtime_capabilities import runtime_capability_snapshot

    snapshot = runtime_capability_snapshot()
    assert snapshot["git_spawn_guard"]["status"] in {"armed", "unarmed", "not_needed"}


class _Api:
    def __init__(self, guard: object) -> None:
        stamp = {"app_version": "0.7.1", "commit": COMMIT[:12], "commit_full": COMMIT, "dirty": False}
        routes = {"/healthz": {"ok": True, "runtime": stamp}, "/api/runtime/version": stamp,
                  "/api/runtime/capabilities": {"mode": "runtime", **({"git_spawn_guard": guard} if guard is not None else {})}}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                raw = json.dumps(routes.get(self.path, {})).encode()
                self.send_response(200 if self.path in routes else 404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _slices(guard: object, tmp_path: Path, *, require: bool = True) -> tuple[int, dict]:
    from tools import installed_slices

    api = _Api(guard)
    try:
        out = tmp_path / "slices.json"
        argv = ["--base-url", api.url, "--expected-commit", COMMIT, "--out", str(out)]
        code = installed_slices.main(argv + (["--require-git-spawn-guard"] if require else []))
        return code, json.loads(out.read_text(encoding="utf-8"))
    finally:
        api.close()


def test_an_armed_guard_passes_the_smoke_slice(tmp_path: Path) -> None:
    code, report = _slices({"status": "armed", "armed": True}, tmp_path)
    row = next(r for r in report["slices"] if r["name"] == "git_spawn_guard")
    assert code == 0 and row["status"] == "pass" and row["control"]["status"] == "fail"


@pytest.mark.parametrize("guard", [
    {"status": "unarmed", "armed": False}, {"status": "not_needed", "armed": False},
    {"status": "armed", "armed": "yes"}, None,
], ids=["unarmed", "not-needed", "not-a-bool", "missing"])
def test_anything_but_armed_is_red(tmp_path: Path, guard) -> None:
    code, report = _slices(guard, tmp_path)
    row = next(r for r in report["slices"] if r["name"] == "git_spawn_guard")
    assert code != 0 and report["status"] == "fail" and row["status"] == "fail"


def test_without_the_flag_the_slices_are_as_before(tmp_path: Path) -> None:
    code, report = _slices(None, tmp_path, require=False)
    assert code == 0 and [r["name"] for r in report["slices"]] == ["health", "version_identity"]


def test_the_windows_install_smoke_requires_the_guard() -> None:
    smoke = (ROOT / "tools" / "windows_install_smoke.ps1").read_text(encoding="utf-8")
    call = smoke[smoke.index("tools\\installed_slices.py"):]
    call = call[:call.index("\n")]
    assert "--require-git-spawn-guard" in call
    assert 'Require "slices" "installed_slices_pass" ($slicesExit -eq 0)' in smoke
