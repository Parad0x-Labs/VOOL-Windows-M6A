from __future__ import annotations

import io
import time

import pytest

from core import native_control
from tests.asgi_harness import asgi_request


@pytest.fixture(autouse=True)
def _clear_native_control(monkeypatch):
    native_control.install_secret(None)
    monkeypatch.delenv("VOOL_WINDOWS_BUNDLE", raising=False)
    yield
    native_control.install_secret(None)


def test_proof_binds_method_path_body_and_is_single_use() -> None:
    secret = b"a" * native_control.SECRET_BYTES
    verifier = native_control.NativeControlVerifier(secret)
    body = b'{"op":"resolve_approval","decision":"allow"}'
    headers = native_control.sign_headers(secret, "POST", "/api/mode", body)

    assert not verifier.verify("POST", "/api/mode", body + b" ", headers)
    assert not verifier.verify("POST", "/api/commands/dispatch", body, headers)
    assert not verifier.verify("POST", "/api/mode?scope=all", body, headers)
    assert not verifier.verify("DELETE", "/api/mode", body, headers)
    assert verifier.verify("POST", "/api/mode", body, headers)
    assert not verifier.verify("POST", "/api/mode", body, headers)


def test_old_proof_and_previous_launch_secret_are_rejected() -> None:
    old = b"a" * native_control.SECRET_BYTES
    body = b"{}"
    expired = native_control.sign_headers(
        old, "POST", "/api/mode", body, issued_at=int(time.time()) - 120
    )
    assert not native_control.NativeControlVerifier(old).verify("POST", "/api/mode", body, expired)

    current = native_control.sign_headers(old, "POST", "/api/mode", body)
    assert not native_control.NativeControlVerifier(b"b" * native_control.SECRET_BYTES).verify(
        "POST", "/api/mode", body, current
    )


def test_windows_bundle_gate_refuses_missing_secret_and_forged_headers(monkeypatch) -> None:
    monkeypatch.setenv("VOOL_WINDOWS_BUNDLE", "1")
    assert not native_control.authorize_http_request("POST", "/api/mode", b"{}", {})
    assert not native_control.authorize_http_request("POST", "/api/commands/dispatch", b"{}", {"Origin": "http://127.0.0.1:11435"})
    assert not native_control.authorize_http_request("POST", "/api/mode?scope=all", b"{}", {})
    assert native_control.authorize_http_request("POST", "/api/chat", b"{}", {})

    secret = b"c" * native_control.SECRET_BYTES
    native_control.install_secret(secret)
    headers = native_control.sign_headers(secret, "POST", "/api/mode", b"{}")
    assert native_control.authorize_http_request("POST", "/api/mode", b"{}", headers)
    assert not native_control.authorize_http_request("POST", "/api/mode", b"{}", headers)


def test_non_bundle_behavior_is_unchanged() -> None:
    assert native_control.authorize_http_request("POST", "/api/mode", b"{}", {})


def test_bootstrap_requires_pipe_and_never_uses_environment_secret(monkeypatch) -> None:
    monkeypatch.delenv("VOOL_NATIVE_CONTROL_STDIN", raising=False)
    monkeypatch.setenv("VOOL_NATIVE_CONTROL_SECRET", "do-not-use")
    with pytest.raises(native_control.NativeControlUnavailable):
        native_control.read_bootstrap_secret()

    monkeypatch.setenv("VOOL_NATIVE_CONTROL_STDIN", "1")
    monkeypatch.setattr(native_control.sys, "stdin", io.TextIOWrapper(io.BytesIO(b"d" * 32), encoding="utf-8"))
    assert native_control.read_bootstrap_secret() == b"d" * 32
    assert "VOOL_NATIVE_CONTROL_STDIN" not in native_control.os.environ


def test_asgi_denies_local_mode_and_command_dispatch_before_handler(monkeypatch) -> None:
    from core.web.api.app import create_api_app
    from core.web.api.service import json_response

    monkeypatch.setenv("VOOL_WINDOWS_BUNDLE", "1")
    calls = []
    app = create_api_app(
        runtime=object(), model_name="test",
        post_dispatcher=lambda **kwargs: calls.append(kwargs) or json_response(200, {"ok": True}),
    )
    body = b'{"op":"resolve_approval","approval_id":"x","decision":"allow"}'
    base = {"Host": "127.0.0.1:11435", "Content-Type": "application/json"}
    for path in ("/api/mode", "/api/commands/dispatch"):
        for headers in (base, {**base, "Origin": "http://127.0.0.1:11435"}):
            status, _, _ = asgi_request(app, method="POST", path=path, headers=headers, body=body)
            assert status == 403
    assert calls == []

    secret = b"e" * native_control.SECRET_BYTES
    native_control.install_secret(secret)
    proof = native_control.sign_headers(secret, "POST", "/api/mode", body)
    status, _, _ = asgi_request(app, method="POST", path="/api/mode", headers={**base, **proof}, body=body)
    assert status == 200 and len(calls) == 1
    status, _, _ = asgi_request(app, method="POST", path="/api/mode", headers={**base, **proof}, body=body)
    assert status == 403 and len(calls) == 1
    query_proof = native_control.sign_headers(secret, "POST", "/api/mode", body)
    status, _, _ = asgi_request(
        app, method="POST", path="/api/mode", query_string=b"scope=all",
        headers={**base, **query_proof}, body=body,
    )
    assert status == 403 and len(calls) == 1
    command_proof = native_control.sign_headers(secret, "POST", "/api/commands/dispatch", body)
    status, _, _ = asgi_request(
        app, method="POST", path="/api/commands/dispatch",
        headers={**base, **command_proof}, body=body,
    )
    assert status == 200 and len(calls) == 2


def test_legacy_http_handler_cannot_bypass_native_gate(monkeypatch) -> None:
    from apps import vool_api_server

    monkeypatch.setenv("VOOL_WINDOWS_BUNDLE", "1")
    body = b'{"op":"resolve_approval"}'
    handler = vool_api_server.VoolAPIHandler.__new__(vool_api_server.VoolAPIHandler)
    handler.path = "/api/mode"
    handler.headers = {
        "Host": "127.0.0.1:11435", "Content-Length": str(len(body)),
        "Content-Type": "application/json",
    }
    handler.rfile = io.BytesIO(body)
    handler.client_address = ("127.0.0.1", 12345)
    handler.server = object()
    responses = []
    handler._write_response = responses.append
    monkeypatch.setattr(vool_api_server, "_legacy_handler_runtime", lambda server: object())
    monkeypatch.setattr(vool_api_server, "_dispatch_post", lambda **kwargs: pytest.fail("dispatcher reached"))

    handler.do_POST()
    assert responses[0].status == 403


def test_legacy_http_handler_accepts_one_signed_command_request(monkeypatch) -> None:
    from apps import vool_api_server
    from core.web.api.service import json_response

    monkeypatch.setenv("VOOL_WINDOWS_BUNDLE", "1")
    secret = b"g" * native_control.SECRET_BYTES
    native_control.install_secret(secret)
    body = b'{"intent":"update.check"}'
    path = "/api/commands/dispatch"
    headers = native_control.sign_headers(secret, "POST", path, body)
    handler = vool_api_server.VoolAPIHandler.__new__(vool_api_server.VoolAPIHandler)
    handler.path = path
    handler.headers = {
        "Host": "127.0.0.1:11435", "Content-Length": str(len(body)),
        "Content-Type": "application/json", **headers,
    }
    handler.rfile = io.BytesIO(body)
    handler.client_address = ("127.0.0.1", 12345)
    handler.server = object()
    responses = []
    handler._write_response = responses.append
    monkeypatch.setattr(vool_api_server, "_legacy_handler_runtime", lambda server: object())
    monkeypatch.setattr(vool_api_server, "_dispatch_post", lambda **kwargs: json_response(200, {"ok": True}))

    handler.do_POST()
    assert responses[0].status == 200
    handler.rfile = io.BytesIO(body)
    handler.do_POST()
    assert responses[1].status == 403


def test_bundle_api_refuses_to_boot_without_its_pipe(monkeypatch) -> None:
    from apps import vool_api_server

    monkeypatch.setenv("VOOL_WINDOWS_BUNDLE", "1")
    monkeypatch.delenv("VOOL_NATIVE_CONTROL_STDIN", raising=False)
    assert vool_api_server.main() == 1


def test_bundle_window_refuses_to_boot_without_its_pipe(monkeypatch) -> None:
    from installer.bundle import vool_window

    monkeypatch.setenv("VOOL_WINDOWS_BUNDLE", "1")
    monkeypatch.delenv("VOOL_NATIVE_CONTROL_STDIN", raising=False)
    monkeypatch.setattr(vool_window, "_single_instance", lambda: pytest.fail("window boot continued"))
    monkeypatch.setattr(vool_window, "_log", lambda message: None)
    assert vool_window.main() == 1
    assert vool_window._NATIVE_CONTROL_SECRET is None


def test_asgi_rejects_oversized_body_before_dispatch(monkeypatch) -> None:
    from core.web.api.app import create_api_app

    monkeypatch.setenv("VOOL_WINDOWS_BUNDLE", "1")
    app = create_api_app(
        runtime=object(), model_name="test",
        post_dispatcher=lambda **kwargs: pytest.fail("dispatcher reached"),
    )
    status, _, _ = asgi_request(
        app, method="POST", path="/api/mode",
        headers={"Host": "127.0.0.1:11435"}, body=b"a" * (4 * 1024 * 1024 + 1),
    )
    assert status == 413


def test_legacy_rejects_oversized_length_without_reading(monkeypatch) -> None:
    from apps import vool_api_server

    class Unreadable:
        def read(self, count):
            pytest.fail("oversized body was read")

    monkeypatch.setenv("VOOL_WINDOWS_BUNDLE", "1")
    handler = vool_api_server.VoolAPIHandler.__new__(vool_api_server.VoolAPIHandler)
    handler.path = "/api/mode"
    handler.headers = {
        "Host": "127.0.0.1:11435", "Content-Length": str(4 * 1024 * 1024 + 1),
        "Content-Type": "application/json",
    }
    handler.rfile = Unreadable()
    handler.client_address = ("127.0.0.1", 12345)
    handler.server = object()
    responses = []
    handler._write_response = responses.append
    monkeypatch.setattr(vool_api_server, "_legacy_handler_runtime", lambda server: object())

    handler.do_POST()
    assert responses[0].status == 413


def test_window_private_control_proof_is_not_exposed_to_page(monkeypatch) -> None:
    from installer.bundle import vool_window

    secret = b"f" * native_control.SECRET_BYTES
    monkeypatch.setattr(vool_window, "_NATIVE_CONTROL_SECRET", secret)
    body = b'{"op":"resolve_approval"}'
    headers = vool_window._native_control_headers("POST", "/api/mode", body)
    assert native_control.NativeControlVerifier(secret).verify("POST", "/api/mode", body, headers)
    assert secret.hex() not in repr(headers)
    assert secret.hex() not in vool_window.URL
    assert not hasattr(vool_window._WindowApi(), "_NATIVE_CONTROL_SECRET")
    assert not hasattr(vool_window._WindowApi(), "_native_control_headers")
