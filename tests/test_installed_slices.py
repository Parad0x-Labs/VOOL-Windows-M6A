"""The installed-slice harness drives a running VOOL over loopback and reports each slice as JSON.

Each slice runs its check against the installed API, then a negative control: the same check
pointed where it must fail (nothing listening, or a commit the build is not). A slice passes only
when its check passes AND its control fails, so a harness that cannot tell good from bad reports
itself broken instead of reporting VOOL healthy. These tests serve a stand-in API on loopback.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tools import installed_slices

ROOT = Path(__file__).resolve().parents[1]
COMMIT = "0123456789abcdef0123456789abcdef01234567"


class _Api:
    def __init__(self, routes: dict[str, tuple[int, object]]) -> None:
        routes_ = routes

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                status, body = routes_.get(self.path, (404, {"error": "not found"}))
                raw = json.dumps(body).encode() if not isinstance(body, bytes) else body
                self.send_response(status)
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


def _healthy(commit: str = COMMIT, *, dirty: bool = False) -> dict[str, tuple[int, object]]:
    stamp = {"app_version": "0.7.1", "commit": commit[:12], "commit_full": commit, "dirty": dirty}
    return {"/healthz": (200, {"ok": True, "runtime": stamp}), "/api/runtime/version": (200, stamp)}


@pytest.fixture()
def api(request):
    server = _Api(request.param if hasattr(request, "param") else _healthy())
    yield server
    server.close()


def _run(url: str, tmp_path: Path, commit: str = COMMIT) -> tuple[int, dict]:
    out = tmp_path / "slices.json"
    code = installed_slices.main(["--base-url", url, "--expected-commit", commit, "--out", str(out)])
    return code, json.loads(out.read_text(encoding="utf-8"))


def _slice(report: dict, name: str) -> dict:
    return next(row for row in report["slices"] if row["name"] == name)


def test_a_healthy_install_of_the_expected_commit_passes_both_slices(api, tmp_path: Path) -> None:
    code, report = _run(api.url, tmp_path)

    assert code == 0
    assert report["schema"] == "vool.installed_slices.v1" and report["status"] == "pass"
    for name in ("health", "version_identity"):
        row = _slice(report, name)
        assert row["status"] == "pass", row
        assert row["control"]["status"] == "fail", row


@pytest.mark.parametrize("api", [{"/healthz": (503, {"ok": False})}], indirect=True)
def test_an_unhealthy_api_fails_the_health_slice(api, tmp_path: Path) -> None:
    code, report = _run(api.url, tmp_path)

    assert code != 0 and report["status"] == "fail"
    assert _slice(report, "health")["status"] == "fail"


@pytest.mark.parametrize("api", [{"/healthz": (200, {"ok": False})}], indirect=True)
def test_a_health_body_that_is_not_ok_fails_the_health_slice(api, tmp_path: Path) -> None:
    _code, report = _run(api.url, tmp_path)

    assert _slice(report, "health")["status"] == "fail"


def test_another_commit_fails_the_version_slice(api, tmp_path: Path) -> None:
    code, report = _run(api.url, tmp_path, commit="f" * 40)

    assert code != 0
    assert _slice(report, "version_identity")["status"] == "fail"
    assert _slice(report, "health")["status"] == "pass"


@pytest.mark.parametrize("api", [_healthy(dirty=True)], indirect=True)
def test_a_dirty_build_fails_the_version_slice(api, tmp_path: Path) -> None:
    _code, report = _run(api.url, tmp_path)

    assert _slice(report, "version_identity")["status"] == "fail"


def test_nothing_listening_fails_every_slice(tmp_path: Path) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    code, report = _run(f"http://127.0.0.1:{port}", tmp_path)

    assert code != 0
    assert {row["status"] for row in report["slices"]} == {"fail"}


def test_a_control_that_passes_marks_the_slice_failed_not_passed(api, tmp_path: Path, monkeypatch) -> None:
    """A check that cannot fail proves nothing: a harness bug must not read as a healthy VOOL."""
    monkeypatch.setattr(installed_slices, "_check_version", lambda base_url, expected: (True, {"forced": True}))

    code, report = _run(api.url, tmp_path)

    row = _slice(report, "version_identity")
    assert code != 0 and row["status"] == "fail"
    assert row["control"]["status"] == "pass"
    assert "control" in row["reason"]


def test_only_loopback_urls_are_driven(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        installed_slices.main(["--base-url", "http://10.0.0.5:11435", "--expected-commit", COMMIT, "--out", str(tmp_path / "x.json")])


def test_it_runs_as_a_script_from_the_repo_root(api, tmp_path: Path) -> None:
    out = tmp_path / "slices.json"
    result = subprocess.run(
        [sys.executable, "tools/installed_slices.py", "--base-url", api.url, "--expected-commit", COMMIT, "--out", str(out)],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(out.read_text(encoding="utf-8"))["status"] == "pass"


def test_the_install_smoke_runs_the_slices_after_launch_and_keeps_their_report() -> None:
    smoke = (ROOT / "tools" / "windows_install_smoke.ps1").read_text(encoding="utf-8")
    launch = smoke.index('Require "launch" "healthz_200"')
    restart = smoke.index("# --- restart")

    call = smoke.index("tools\\installed_slices.py")
    assert launch < call < restart
    assert "slices.json" in smoke
