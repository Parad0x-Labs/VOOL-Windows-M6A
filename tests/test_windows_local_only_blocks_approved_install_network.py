"""On Windows, Local Only keeps an operator-approved install off the network.

Mac 0.7.3 lets an install the operator approved reach the network (`_network_grant_reason`). Local
Only allows no public egress but browser search, so on Windows that grant is never given while Local
Only binds the turn. With Local Only off, the same approved install still gets its grant (control).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import core.effect_gateway as effect_gateway
import core.remote_fetch_policy as remote_fetch_policy
from core.mode_permission_policy import OperatingMode
from sandbox import network_guard, sandbox_runner


@pytest.fixture
def approved_install(monkeypatch):
    monkeypatch.setattr(network_guard, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        effect_gateway, "consume_turn_policy", lambda *_args, **_kwargs: SimpleNamespace(mode="manual")
    )
    authorization = SimpleNamespace(operator_granted=True, mode=OperatingMode.MANUAL)
    monkeypatch.setattr(
        sandbox_runner.ExecutionGate,
        "_covering_authorization",
        staticmethod(lambda _cmd, *, policy_mode: authorization),
    )
    return "pip install requests"


def test_local_only_gives_an_approved_install_no_network(monkeypatch, approved_install) -> None:
    monkeypatch.setattr(remote_fetch_policy, "local_only_active", lambda *_args, **_kwargs: True)
    assert sandbox_runner._network_grant_reason(approved_install) == ""


def test_an_unreadable_local_only_state_gives_no_network(monkeypatch, approved_install) -> None:
    def unreadable(*_args, **_kwargs):
        raise RuntimeError("policy store unreadable")

    monkeypatch.setattr(remote_fetch_policy, "local_only_active", unreadable)
    assert sandbox_runner._network_grant_reason(approved_install) == ""


def test_without_local_only_the_approved_install_keeps_its_grant(monkeypatch, approved_install) -> None:
    monkeypatch.setattr(remote_fetch_policy, "local_only_active", lambda *_args, **_kwargs: False)
    assert sandbox_runner._network_grant_reason(approved_install) == "you approved this command"
