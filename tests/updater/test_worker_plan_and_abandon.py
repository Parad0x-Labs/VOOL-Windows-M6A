"""Seams between the API, the bundle supervisor and the detached Windows update worker.

E2-seams-02#1: plan.json must not pick the worker's trust branch, and the worker must run the
exact plan the supervisor validated. E2-seams-02#2: a request the API abandoned on timeout must
not start a worker, even when the supervisor had already taken it.
"""
import hashlib
import json
from pathlib import Path

import pytest

import core.updater.worker as worker_module
from core.updater.windows import BUNDLE_MANIFEST_SCHEMA, WORKER_REQUEST_NAME, consume_worker_request
from core.updater.worker import WINDOWS_WORKER_APP_ENV, plan_trust, request_supervisor_start
from tests.updater.helpers import generate_publisher_keypair

TXID = "tx-seams-02"
PLAN_DIGEST_ENV = "VOOL_UPDATE_WORKER_PLAN_SHA256"


def _darwin_plan(public_hex: str) -> dict:
    return {"platform": "darwin-arm64", "keys": {"attacker": public_hex},
            "feed": {"app_path": "C:\\elsewhere"}}


def test_worker_on_windows_refuses_a_non_windows_plan_without_the_app_env(monkeypatch):
    monkeypatch.setattr(worker_module, "_is_windows", lambda: True)
    _, public_hex = generate_publisher_keypair()
    with pytest.raises(ValueError, match="another platform"):
        plan_trust(_darwin_plan(public_hex), env={})


def test_worker_refuses_a_plan_rewritten_after_the_supervisor_checked_it():
    from core.updater.worker import check_plan_digest

    validated = json.dumps({"txid": TXID, "platform": "windows-x64"}).encode("utf-8")
    rewritten = json.dumps({"txid": TXID, "platform": "darwin-arm64"}).encode("utf-8")
    env = {WINDOWS_WORKER_APP_ENV: "C:\\app", PLAN_DIGEST_ENV: hashlib.sha256(validated).hexdigest()}
    check_plan_digest(validated, env=env)
    with pytest.raises(ValueError, match="not the one the VOOL supervisor validated"):
        check_plan_digest(rewritten, env=env)
    with pytest.raises(ValueError, match="not the one the VOOL supervisor validated"):
        check_plan_digest(validated, env={WINDOWS_WORKER_APP_ENV: "C:\\app"})


def _installed_bundle(tmp_path: Path) -> Path:
    app_root = (tmp_path / "program").resolve()
    (app_root / "python").mkdir(parents=True)
    (app_root / "python" / "python.exe").write_bytes(b"")
    (app_root / "app" / "core").mkdir(parents=True)
    (app_root / "bundle_manifest.json").write_text(json.dumps({"schema": BUNDLE_MANIFEST_SCHEMA}),
                                                   encoding="utf-8")
    return app_root


def _prepared_job(home: Path, app_root: Path) -> Path:
    job = (home / "data" / "update_v2").resolve() / "workers" / TXID
    job.mkdir(parents=True)
    plan = job / "plan.json"
    plan.write_text(json.dumps({"txid": TXID, "platform": "windows-x64",
                                "feed": {"app_path": str(app_root)}}), encoding="utf-8")
    (home / "run").mkdir(parents=True, exist_ok=True)
    (home / "run" / WORKER_REQUEST_NAME).write_text(json.dumps({"txid": TXID}), encoding="utf-8")
    return plan


class _FakeWorker:
    pid = 54321


class _RecordingPopen:
    def __init__(self) -> None:
        self.calls: list[tuple[list, dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        return _FakeWorker()


def _give_up_now(updater_root: Path, plan: Path) -> None:
    request_supervisor_start(updater_root, TXID, plan, clock=lambda: 0.0,
                             sleeper=lambda _seconds: None, timeout=0.0)


def test_timed_out_request_records_the_abandon_in_the_job_folder(tmp_path):
    home = tmp_path / "home"
    plan = _prepared_job(home, _installed_bundle(tmp_path))
    with pytest.raises(RuntimeError, match="nothing was installed"):
        _give_up_now(home / "data" / "update_v2", plan)
    claim = json.loads((plan.parent / "start-claim.json").read_text(encoding="utf-8"))
    assert claim["txid"] == TXID and claim["outcome"] == "abandoned"
    assert not (home / "run" / WORKER_REQUEST_NAME).exists()


def test_request_abandoned_during_the_interpreter_copy_never_starts_the_worker(tmp_path):
    home = tmp_path / "home"
    app_root = _installed_bundle(tmp_path)
    plan = _prepared_job(home, app_root)
    updater_root = home / "data" / "update_v2"
    popen = _RecordingPopen()
    gave_up: list[bool] = []

    def slow_copytree(*_args, **_kwargs):
        if not gave_up:
            # The API reaches its deadline while the supervisor is still copying the interpreter.
            with pytest.raises(RuntimeError):
                _give_up_now(updater_root, plan)
            gave_up.append(True)

    assert consume_worker_request(home, app_root, popen=popen, copytree=slow_copytree,
                                  now=lambda: 1.0) is None
    assert gave_up == [True]
    assert popen.calls == []
    assert not (updater_root / "worker.json").exists()


def test_supervisor_pins_the_plan_bytes_it_validated(tmp_path):
    home = tmp_path / "home"
    app_root = _installed_bundle(tmp_path)
    plan = _prepared_job(home, app_root)
    popen = _RecordingPopen()
    assert consume_worker_request(home, app_root, popen=popen, copytree=lambda *_a, **_k: None,
                                  now=lambda: 1.0) == _FakeWorker.pid
    assert popen.calls[0][1]["env"][PLAN_DIGEST_ENV] == hashlib.sha256(plan.read_bytes()).hexdigest()
