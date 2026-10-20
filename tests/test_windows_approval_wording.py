"""On Windows the approval words say what Windows does; on a Mac they stay exactly as they were.

What executes: the served pages (core.web.api.service.dispatch_get for /chat and /settings), the Contacts protection door
(POST /api/contacts/credential/enroll -> core.operator_credential.authority -> core.os_consent_gate) and the wallet doors
(POST /api/wallet/pocket/create, /api/wallet/approval-method and /api/wallet/export -> core.wallet.custody).

The host is simulated by patching sys.platform, the check the consent gate routes its prompt by and the one these words
are chosen by, and only for the duration of each call. On "win32" the consent gate's own Windows path runs: no Windows
Hello bridge (the bundle installs no winsdk or winrt, so both are made unimportable here) and then the credential window.
Its DLL is the one stand-in, because credui.dll cannot load off Windows; it answers what the real window returns for OK
(0) and Cancel (ERROR_CANCELLED, 1223). The wallet runs with no device authentication, which is what Windows has
(core.wallet.device_auth needs Darwin), through the existing test authority seam, so no host's Keychain is touched.
"""
from __future__ import annotations

import ctypes
import json
import sys
from collections.abc import Callable
from typing import Any

import pytest

from core import os_consent_gate
from core.operator_credential import authority as credential
from core.web.api.runtime import RuntimeServices
from core.web.api.service import dispatch_get, dispatch_post
from tests._windows_served_words import CONTACTS_WORDS, WALLET_WORDS, as_the_mac_page
from tests.contacts import protected_changes
from tests.wallet._device_auth_fake import FakeDeviceAuthority
from tests.wallet._rig import rpc, wallet_env

WINDOWS_DENIED = "The Windows confirmation was cancelled or failed; nothing changed."
MAC_DENIED = "The Mac's own confirmation was declined or failed; nothing changed."
CONSENT_UNAVAILABLE = ("This device could not show its own confirmation prompt, so nothing changed. Open VOOL on the device itself and use "
                       "Home → Contacts → Protection.")
ERROR_CANCELLED = 1223
CREDUIWIN_GENERIC = 0x1

WALLET_HEADERS = {"Host": "127.0.0.1", "Content-Type": "application/json", "Origin": "http://127.0.0.1"}


def _as_host(platform: str, call: Callable[[], Any]) -> Any:
    """Run one call as if this runtime served on ``platform``; sys.platform is restored before anything else runs."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sys, "platform", platform)
        return call()


def _page(path: str, platform: str) -> str:
    response = _as_host(platform, lambda: dispatch_get(path=path, query={}, runtime=RuntimeServices(display_name="VOOL"), model_name="vool",
                                                       client_host="127.0.0.1"))
    assert response.status == 200, path
    return response.body.decode("utf-8")


def _post(path: str, body: dict[str, Any], *, headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    response = dispatch_post(path=path, body=body, headers=headers or {"content-type": "application/json"}, runtime=RuntimeServices(display_name="VOOL"),
                             model_name="vool", workspace_root_provider=lambda: "/tmp", client_host="127.0.0.1")
    return response.status, json.loads(response.body.decode("utf-8") or "{}")


def _without(body: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: value for key, value in body.items() if key not in keys}


class _CredentialWindow:
    """credui.dll as the gate loads it: one CredUIPromptForWindowsCredentialsW that returns what the person chose."""

    def __init__(self, answer: int) -> None:
        self.answer = answer
        self.flags: list[int] = []

    def CredUIPromptForWindowsCredentialsW(self, *args: Any) -> int:
        self.flags.append(int(args[-1]))
        return self.answer


@pytest.fixture
def no_consent_override(monkeypatch):
    protected_changes.reset()
    monkeypatch.delenv("VOOL_WALLET_SKIP_CONSENT_GATE", raising=False)
    os_consent_gate.set_consent_override_for_tests(None)
    yield
    os_consent_gate.set_consent_override_for_tests(None)
    protected_changes.reset()


@pytest.fixture
def no_device_authentication(wallet_env):
    from core.wallet import device_auth

    device_auth.set_device_authority_for_tests(FakeDeviceAuthority(available=False))
    yield
    device_auth.set_device_authority_for_tests(None)


def _windows_enroll(window: _CredentialWindow) -> tuple[int, dict[str, Any]]:
    """Enrol through the door on Windows: no Windows Hello bridge, then the credential window."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setitem(sys.modules, "winsdk", None)
        patch.setitem(sys.modules, "winrt", None)
        patch.setattr(ctypes, "WinDLL", lambda name: window if name == "credui.dll" else None, raising=False)
        return _as_host("win32", lambda: _post("/api/contacts/credential/enroll", {"kind": "pin", "secret": protected_changes.SYNTHETIC_PIN}))


# --- Contacts protection ------------------------------------------------------------------------------------------------

def test_the_served_contacts_protection_names_the_windows_confirmation_window_on_windows_and_keeps_the_mac_words() -> None:
    windows, mac = _page("/chat", "win32"), _page("/chat", "darwin")
    for mac_text, windows_text, drawn in CONTACTS_WORDS:
        assert windows.count(windows_text) == drawn and mac_text not in windows, windows_text
        assert mac.count(mac_text) == drawn and windows_text not in mac, mac_text
    assert "this Mac to confirm" not in windows and "This Mac asks you" not in windows


def test_on_windows_cancel_in_the_confirmation_window_says_so_and_changes_nothing_and_ok_continues(no_consent_override) -> None:
    cancelled = _CredentialWindow(ERROR_CANCELLED)
    status, body = _windows_enroll(cancelled)
    assert cancelled.flags == [CREDUIWIN_GENERIC], "the credential window was shown once, as the generic prompt"
    assert (status, body) == (403, {"ok": False, "reason": "consent_denied", "message": WINDOWS_DENIED}), body
    assert credential.status()["enrolled"] is False, "a cancelled window changes nothing"

    # a window that answers neither OK nor Cancel did not open: that fails closed with its own words, never as a cancel
    broken = _CredentialWindow(5)
    status, body = _windows_enroll(broken)
    assert broken.flags == [CREDUIWIN_GENERIC]
    assert status == 409 and body["reason"] == "consent_unavailable" and body["message"] == CONSENT_UNAVAILABLE, body
    assert credential.status()["enrolled"] is False

    # the window's OK is what continues: the same request now enrols
    ok = _CredentialWindow(0)
    status, body = _windows_enroll(ok)
    assert ok.flags == [CREDUIWIN_GENERIC]
    assert status == 200 and body["status"] == "enrolled", body
    assert credential.status()["enrolled"] is True


def test_a_refused_confirmation_is_the_same_answer_on_windows_and_a_mac_apart_from_its_words(no_consent_override) -> None:
    def refused(answer: Callable[[str], bool]) -> Callable[[], tuple[int, dict[str, Any]]]:
        def call() -> tuple[int, dict[str, Any]]:
            os_consent_gate.set_consent_override_for_tests(answer)
            try:
                return _post("/api/contacts/credential/enroll", {"kind": "pin", "secret": protected_changes.SYNTHETIC_PIN})
            finally:
                os_consent_gate.set_consent_override_for_tests(None)
        return call

    def declined(_reason: str) -> bool:
        raise os_consent_gate.ConsentDeniedError("declined")

    for answer in (declined, lambda _reason: False):
        windows_status, windows = _as_host("win32", refused(answer))
        mac_status, mac = _as_host("darwin", refused(answer))
        assert windows["message"] == WINDOWS_DENIED and mac["message"] == MAC_DENIED, (windows, mac)
        assert windows_status == mac_status == 403
        assert _without(windows, "message") == _without(mac, "message") == {"ok": False, "reason": "consent_denied"}
        assert credential.status()["enrolled"] is False


# --- the wallet -----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/settings", "/chat"])
def test_the_served_wallet_never_promises_touch_id_on_windows_where_it_draws_device_approval(path: str) -> None:
    windows, mac = _page(path, "win32"), _page(path, "darwin")
    for mac_text, windows_text, drawn in WALLET_WORDS:
        assert windows.count(windows_text) == drawn and mac_text not in windows, windows_text
        assert mac.count(mac_text) == drawn and windows_text not in mac, mac_text
    # wording only: the same three methods stay offered for a switch, and nothing else on the page differs
    assert "['pin', 'password', 'device'].forEach" in windows
    assert as_the_mac_page(windows) == mac, "Windows and the Mac are served the same page apart from the Windows words"


def test_on_windows_the_export_refusal_says_export_is_not_available_there_and_is_otherwise_the_mac_answer(no_device_authentication) -> None:
    from core.wallet import custody

    def create(method: str) -> tuple[int, dict[str, Any]]:
        return _post("/api/wallet/pocket/create", {"acknowledged_warning": True, "confirmation_phrase": custody.POCKET_CONFIRMATION_PHRASE,
                                                   "approval_method": method, "pin": "4829"}, headers=WALLET_HEADERS)

    # the device choice the page labels as not available on Windows is refused there; a PIN wallet is made
    status, refused = _as_host("win32", lambda: create("device"))
    assert status == 403 and refused["error"] == "wallet_device_auth_unavailable", refused
    status, created = _as_host("win32", lambda: create("pin"))
    assert status == 200 and created["wallet"]["approval_method"] == "pin", created
    wallet_id = created["wallet"]["wallet_id"]
    status, switched = _as_host("win32", lambda: _post("/api/wallet/approval-method", {"wallet_id": wallet_id, "method": "device", "pin": "4829"},
                                                       headers=WALLET_HEADERS))
    assert status == 403 and switched["error"] == "wallet_device_auth_unavailable", switched

    def export() -> tuple[int, dict[str, Any]]:
        return _post("/api/wallet/export", {"wallet_id": wallet_id, "target": "phantom"}, headers=WALLET_HEADERS)

    windows_status, windows = _as_host("win32", export)
    mac_status, mac = _as_host("darwin", export)
    windows_context, mac_context = windows["fault"]["context"], mac["fault"]["context"]
    assert windows_context["remediation"] == ("This wallet has no device-protected copy, and export unlocks only with device approval on a Mac, "
                                              "which is not available on Windows. Your recovery phrase still restores this wallet."), windows
    assert "Touch ID" not in windows_context["remediation"] and "this Mac" not in windows_context["remediation"]
    assert mac_context["remediation"] == ("This wallet was created on a machine without device authentication (Touch ID or password). "
                                          "Restore it from its recovery phrase on this Mac to enable export."), mac
    assert windows_status == mac_status == 403
    assert windows["error"] == mac["error"] == "wallet_device_auth_unavailable" and windows_context["reason"] == "wallet_not_device_bound"
    assert _without(windows, "fault") == _without(mac, "fault")
    assert _without(windows["fault"], "fault_id", "context") == _without(mac["fault"], "fault_id", "context")
    assert _without(windows_context, "remediation") == _without(mac_context, "remediation")
