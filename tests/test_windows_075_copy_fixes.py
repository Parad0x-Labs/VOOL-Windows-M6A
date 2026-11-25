"""0.7.5 copy fixes: the /freeze reply names the wallet freeze, and the Windows refusals name no version.

On Windows the in-chat brake is the wallet freeze that /unfreeze lifts (the same words the send
refusal uses: "Type /unfreeze to lift it."), so its replies say that instead of "x402 agent
stopped ... Say /startx402". Off Windows the Mac copy is unchanged, byte for byte.

The Windows refusals for pip, other programs, other interpreters, plugins, MCP servers and git
said "in 0.7.1" / "VOOL 0.7.1"; they now say what Windows refuses without naming a version.
"""
from __future__ import annotations

import dataclasses

import pytest

import core.wallet_spend_policy_store as store
from core import windows_write_gate
from core.vool_agent_brake import maybe_handle_agent_brake
from core.wallet import limits
from core.wallet_spend_policy import SpendLedger, SpendPolicy

MAC_STOP = (
    "x402 agent stopped. Spending is now frozen -- no x402 payment or .null "
    "registration can go through until you resume. Your assistant and local model keep "
    "running. Say /startx402 to resume."
)
MAC_ALREADY = (
    "x402 spending was already frozen. No payment or .null registration can go "
    "through until you resume. Say /startx402 to resume."
)
MAC_NOT_OWNER = (
    "Resuming spending can only be done from your own local session. Spending "
    "stays frozen. Say /startx402 from your local VOOL session to resume."
)
MAC_DECLINED = (
    "Resume was declined at the OS prompt, so spending stays frozen. Say /startx402 "
    "and approve the prompt when you want to resume."
)
MAC_ALREADY_ACTIVE = "x402 spending was already active. Per-tx and daily/weekly caps still apply."
MAC_RESUMED = (
    "x402 agent resumed. Spending is unfrozen; your per-tx and daily/weekly caps still "
    "apply, and each spend is still gated by an OS approval prompt."
)


class _State:
    def __init__(self, frozen: bool = False) -> None:
        self.policy = SpendPolicy(frozen=frozen)
        self.ledger = SpendLedger()


@pytest.fixture(autouse=True)
def _canonical_thawed():
    limits.set_frozen(False)
    yield
    limits.set_frozen(False)


def _wire(monkeypatch, state: _State, *, windows: bool) -> None:
    monkeypatch.setattr(windows_write_gate, "_IS_WINDOWS", windows)
    monkeypatch.setattr(
        store, "load_policy_and_ledger",
        lambda wallet, **_kw: (dataclasses.replace(state.policy), SpendLedger()),
    )

    def _save(wallet, policy, ledger, *, now=None):
        state.policy = policy

    monkeypatch.setattr(store, "save_policy_and_ledger", _save)
    limits.set_frozen(state.policy.frozen)


def _say(text: str, *, consent=lambda _r: True, owner_local: bool = True) -> str:
    result = maybe_handle_agent_brake(
        text, "sess-1", wallet_fn=None, consent_fn=consent, owner_local=owner_local,
    )
    assert result is not None
    return result["response"]


def _all_windows_brake_replies(monkeypatch) -> list[str]:
    replies = []
    state = _State(frozen=False)
    _wire(monkeypatch, state, windows=True)
    replies.append(_say("/freeze"))
    replies.append(_say("/freeze"))
    replies.append(_say("/unfreeze", owner_local=False))
    replies.append(_say("/unfreeze", consent=lambda _r: False))
    replies.append(_say("/unfreeze"))
    replies.append(_say("/unfreeze"))
    return replies


# -- the brake on Windows ---------------------------------------------------------------------


def test_on_windows_freeze_names_the_wallet_freeze_and_unfreeze(monkeypatch) -> None:
    _wire(monkeypatch, _State(frozen=False), windows=True)
    text = _say("/freeze")
    assert "wallet freeze" in text.lower()
    assert "/unfreeze" in text
    assert "x402 agent stopped" not in text and "/startx402" not in text
    assert limits.is_frozen() is True


def test_on_windows_freeze_when_already_frozen_says_so_and_names_unfreeze(monkeypatch) -> None:
    _wire(monkeypatch, _State(frozen=True), windows=True)
    text = _say("/freeze")
    assert "already frozen" in text.lower()
    assert "/unfreeze" in text and "/startx402" not in text


def test_on_windows_no_brake_reply_names_x402_or_startx402(monkeypatch) -> None:
    for text in _all_windows_brake_replies(monkeypatch):
        assert "/startx402" not in text, text
        assert "x402" not in text, text


def test_on_windows_unfreeze_replies_keep_their_meaning(monkeypatch) -> None:
    stop, already, not_owner, declined, resumed, already_active = _all_windows_brake_replies(monkeypatch)
    assert "local session" in not_owner and "/unfreeze" in not_owner
    assert "declined" in declined and "/unfreeze" in declined
    assert "lifted" in resumed.lower() and "caps still apply" in resumed
    assert "already active" in already_active.lower()


# -- the brake off Windows: exactly as Mac ships it ------------------------------------------


def test_off_windows_every_brake_reply_is_the_mac_text(monkeypatch) -> None:
    state = _State(frozen=False)
    _wire(monkeypatch, state, windows=False)
    assert _say("/freeze") == MAC_STOP
    assert _say("/freeze") == MAC_ALREADY
    assert _say("/unfreeze", owner_local=False) == MAC_NOT_OWNER
    assert _say("/unfreeze", consent=lambda _r: False) == MAC_DECLINED
    assert _say("/unfreeze") == MAC_RESUMED
    assert _say("/unfreeze") == MAC_ALREADY_ACTIVE


# -- the Windows refusals name no version -----------------------------------------------------


def test_windows_refusal_texts_name_no_version() -> None:
    from core import plugin_executor, safe_git
    from sandbox import job_runner

    texts = {
        "plugins/MCP": plugin_executor.WINDOWS_PLUGIN_MCP_REFUSAL,
        "git": safe_git.GIT_REFUSED_TEXT,
        "program": job_runner.WINDOWS_PROGRAM_REFUSED_TEXT,
        "other python": job_runner.WINDOWS_OTHER_PYTHON_REFUSED_TEXT,
    }
    for name, text in texts.items():
        assert "0.7.1" not in text, f"{name}: {text}"
        assert "on windows" in text.lower(), f"{name}: {text}"
        assert "WSL2/Linux" in text, f"{name}: {text}"


def test_windows_pip_refusal_names_no_version() -> None:
    from sandbox import job_runner

    with pytest.raises(job_runner.KernelIsolationUnavailableError) as refused:
        job_runner.JobRunner._windows_phase1_argv(["python", "-Im", "pip", "install", "x"])
    text = str(refused.value)
    assert "refuses pip, ruff, venv" in text
    assert "0.7.1" not in text, text
    assert "WSL2/Linux" in text


# -- the canned answer to "what does /stopx402 do?" -------------------------------------------

MAC_STOPX402_ANSWER = (
    "`/stopx402` instantly freezes the wallet's `.null`-registration lane, so no `.null` registration "
    "(a SOL spend) can go through until you `/startx402`. Separately, the USDC x402 spend lane is "
    "disabled in this build. The assistant keeps running throughout."
)


def _stopx402_answer(monkeypatch, *, windows: bool) -> str:
    from core import web0_project_grounding

    monkeypatch.setattr(windows_write_gate, "_IS_WINDOWS", windows)
    result = web0_project_grounding._vool_control_response("what does /stopx402 do?")
    assert result is not None and result["intent"] == "vool_stopx402_control"
    return result["response"]


def test_on_windows_the_stopx402_answer_names_the_wallet_freeze_and_unfreeze(monkeypatch) -> None:
    text = _stopx402_answer(monkeypatch, windows=True)
    assert "wallet freeze" in text and "/unfreeze" in text
    assert "/startx402" not in text


def test_off_windows_the_stopx402_answer_is_the_mac_text(monkeypatch) -> None:
    assert _stopx402_answer(monkeypatch, windows=False) == MAC_STOPX402_ANSWER
