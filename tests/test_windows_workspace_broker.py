from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import runtime_execution_tools as runtime_tools
from core import windows_workspace_broker as broker
from core.mode_permission_policy import (
    PermissionEffect,
    decide_tool_call,
    reset_mode_permission_state,
    resolve_approval,
    set_active_mode,
)


@pytest.fixture(autouse=True)
def _clean_permission_state() -> None:
    reset_mode_permission_state()
    yield
    reset_mode_permission_state()


def _context(root: Path) -> dict[str, object]:
    set_active_mode("workspace-broker-test", "manual", client_turn_id="turn-workspace-broker")
    return {
        "runtime_session_id": "workspace-broker-test",
        "operating_mode": "manual",
        "workspace_root": str(root),
    }


def test_workspace_write_approval_binds_prior_state_and_does_not_replay_after_change(tmp_path: Path) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("before", encoding="utf-8")
    context = _context(tmp_path)
    arguments = {"path": "notes.txt", "content": "after"}

    pending = decide_tool_call(
        intent="workspace.write_file",
        arguments=arguments,
        task_id="turn-workspace-broker",
        source_context=context,
    )
    request = pending.approval_request
    assert pending.effect is PermissionEffect.REQUIRE_APPROVAL
    assert request is not None
    binding = request["effect_binding"]
    assert binding["prior_state"] == "file"
    assert binding["prior_sha256"]
    assert binding["tool"] == "workspace.write_file"
    assert binding["content_sha256"] == hashlib.sha256(b"after").hexdigest()
    assert binding["issued_at"] <= binding["expires_at"]
    assert "content_base64" not in json.dumps(request)
    assert request["scope_options"] == ["once"]

    assert resolve_approval(request["approval_id"], decision="allow")
    target.write_text("changed", encoding="utf-8")
    resumed = {**context, "mode_approval_token": request["approval_id"]}
    changed = decide_tool_call(
        intent="workspace.write_file",
        arguments=arguments,
        task_id="turn-workspace-broker",
        source_context=resumed,
    )
    assert changed.effect is PermissionEffect.REQUIRE_APPROVAL


def test_workspace_replace_approval_binds_result_bytes_and_is_always_one_shot(tmp_path: Path) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("before", encoding="utf-8")
    context = _context(tmp_path)
    arguments = {"path": "notes.txt", "old_text": "before", "new_text": "after"}

    pending = decide_tool_call(
        intent="workspace.replace_in_file",
        arguments=arguments,
        task_id="turn-workspace-broker",
        source_context=context,
    )
    request = pending.approval_request
    assert pending.effect is PermissionEffect.REQUIRE_APPROVAL
    assert request is not None
    assert request["effect"] == "workspace_write_v1"
    assert request["scope_options"] == ["once"]
    assert request["effect_binding"]["tool"] == "workspace.replace_in_file"
    assert request["effect_binding"]["content_sha256"] == hashlib.sha256(b"after").hexdigest()

    resolved = resolve_approval(request["approval_id"], decision="allow", scope="request")
    assert resolved and resolved["scope"] == "once"


def test_missing_native_broker_fails_closed_without_touching_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "new.txt"
    monkeypatch.setattr(broker, "_windows_broker_path", lambda: None)

    result = broker.run_workspace_write_broker(
        target=target,
        workspace_root=tmp_path,
        content="must not land",
        source_context={"_execution_identity": {"request_id": "req-1"}},
        session_id="session-1",
        approval_id="approval-1",
    )

    assert result["ok"] is False
    assert result["status"] == "confinement_unavailable"
    assert not target.exists()


def test_identity_bound_windows_write_routes_through_broker_and_never_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "guarded.txt"
    calls: list[dict[str, object]] = []

    def fake_broker(**kwargs):
        calls.append(kwargs)
        content = str(kwargs["content"])
        content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
        target.write_text(content, encoding="utf-8")
        return {
            "ok": True,
            "status": "executed",
            "final_sha256": content_sha256,
            "atomic_installed": True,
            "confinement": {
                "platform": "windows",
                "broker_type": "appcontainer_job_object",
                "effect": "workspace_write_v1",
                "tool": "workspace.write_file",
                "operation_id": "test-operation",
                "relative_target": "guarded.txt",
                "prior_state": "absent",
                "prior_sha256": "",
                "content_sha256": content_sha256,
                "content_length": len(content.encode("utf-8")),
                "appcontainer_name": "Vool.Broker.v1.test",
                "appcontainer_sid": "S-1-15-2-TEST",
                "process_id": 1234,
                "job_owned": True,
                "filesystem_grants": [
                    {
                        "access": "allow",
                        "rights": 2032127,
                        "inheritance": "ContainerInherit,ObjectInherit",
                        "propagation": "None",
                    }
                ],
                "network_policy": "none",
                "timeout_ms": 30000,
                "result": "executed",
                "verified": True,
                "exit_code": 0,
                "timed_out": False,
                "executable_sha256": "a" * 64,
                "token": {
                    "is_app_container": True,
                    "has_restrictions": True,
                    "integrity_level_rid": 4096,
                    "integrity_level": "low",
                    "restricted_sid_count": 1,
                    "capabilities": [],
                },
                "job": {
                    "limit_flags": 1,
                    "active_process_limit": 16,
                    "process_memory_limit_bytes": 268435456,
                    "job_memory_limit_bytes": 536870912,
                    "per_process_user_time_limit_100_ns": 300000000,
                    "cpu_control_flags": 1,
                    "cpu_rate": 5000,
                },
            },
        }

    monkeypatch.setattr(broker, "run_workspace_write_broker", fake_broker)
    result = runtime_tools._write_file(
        {"path": "guarded.txt", "content": "brokered"},
        workspace_root=tmp_path,
        session_id="session-1",
        source_context={"_execution_identity": {"request_id": "req-1"}},
    )

    assert result.ok is True
    assert len(calls) == 1
    assert calls[0]["tool"] == "workspace.write_file"
    assert target.read_text(encoding="utf-8") == "brokered"


def test_identity_bound_windows_write_refuses_without_broker_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "guarded.txt"
    monkeypatch.setattr(
        broker,
        "run_workspace_write_broker",
        lambda **_kwargs: {"ok": False, "status": "confinement_unavailable", "error": "missing"},
    )

    result = runtime_tools._write_file(
        {"path": "guarded.txt", "content": "must not land"},
        workspace_root=tmp_path,
        session_id="session-1",
        source_context={"_execution_identity": {"request_id": "req-1"}},
    )

    assert result.ok is False
    assert result.status == "confinement_unavailable"
    assert not target.exists()


def test_workspace_write_transport_uses_stdin_and_returns_broker_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "new.txt"
    seen: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = list(argv)
        seen["payload"] = json.loads(kwargs["input"])
        seen["env"] = kwargs["env"]
        return SimpleNamespace(
            stdout=json.dumps(
                {
                    "ok": True,
                    "status": "executed",
                    "final_sha256": broker._sha256_bytes(b"secret content"),
                }
            ),
            stderr="",
            returncode=0,
        )

    monkeypatch.setattr(broker, "_windows_broker_path", lambda: Path("C:/diagnostic/vool-broker.exe"))
    monkeypatch.setattr(broker, "_windows_broker_environment", lambda: {})
    monkeypatch.setattr(broker.subprocess, "run", fake_run)

    result = broker.run_workspace_write_broker(
        target=target,
        workspace_root=tmp_path,
        content="secret content",
        source_context={"_execution_identity": {"request_id": "req-1", "execution_id": "exec-1"}},
        session_id="session-1",
        approval_id="approval-1",
    )

    assert result["ok"] is True
    assert seen["argv"] == [str(Path("C:/diagnostic/vool-broker.exe")), "workspace"]
    payload = seen["payload"]
    assert isinstance(payload, dict)
    assert payload["content_base64"] == base64.b64encode(b"secret content").decode("ascii")
    assert "secret content" not in " ".join(str(item) for item in seen["argv"])
    assert payload["effect"] == "workspace_write_v1"
    assert payload["tool"] == "workspace.write_file"
    assert payload["approval_status"] == "consumed"
    assert payload["runtime_id"] == "req-1|exec-1"
    assert seen["env"]["TEMP"] == str(tmp_path)
    assert seen["env"]["TMP"] == str(tmp_path)
