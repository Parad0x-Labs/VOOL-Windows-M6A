"""
core/vool_agent_brake.py
=========================
In-chat emergency brake for the x402 agent -- stop it from spending without killing
the assistant.

The "x402 agent" is not a separate process; its money-touching action is an x402
payment, and every payment is gated by the wallet SpendPolicy's ``frozen`` flag
(see core/wallet_spend_policy.check_spend_allowed). So the brake is a FREEZE: it flips
that persisted, HMAC-authenticated flag, which blocks every subsequent x402 spend and
.null registration instantly. The API, chat, and local model keep running -- only
spending stops -- so the user can still talk to VOOL and resume with one command.

This is deliberately NOT a process kill: a full shutdown of every VOOL process is a
separate, heavier brake. Freezing is the money-safety stop and is fully reversible.

Matching is full-string anchored, like the register command surface: only a clean,
unambiguous brake command fires. "should I stop the agent?" carries extra words and
falls through to the normal responder rather than freezing the wallet.
"""
from __future__ import annotations

import os
import re
import threading
import time
from collections.abc import Callable
from typing import Any

# Full-string anchored so only an explicit brake command fires (never a question that
# merely mentions stopping). Slash forms plus a few natural phrasings for the same intent.
_STOP_RE = re.compile(
    r"^\s*(?:please\s+)?(?:"
    r"/?stop[\s_-]?x402(?:[\s_-]?agent)?"
    r"|/?freeze(?:\s+(?:the\s+)?(?:wallet|spending|spend|agent|x402))?"
    r"|stop\s+(?:the\s+)?(?:x402\s+)?(?:agent|spending|spend)"
    r"|pause\s+(?:the\s+)?(?:x402\s+)?agent"
    r"|emergency\s+stop|panic(?:\s+stop)?"
    r")\s*(?:[.!]+\s*)?$",
    re.IGNORECASE,
)
_START_RE = re.compile(
    r"^\s*(?:please\s+)?(?:"
    r"/?start[\s_-]?x402(?:[\s_-]?agent)?"
    r"|/?resume[\s_-]?x402(?:[\s_-]?agent)?"
    r"|/?unfreeze(?:\s+(?:the\s+)?(?:wallet|spending|spend|agent|x402))?"
    r"|resume\s+(?:the\s+)?(?:x402\s+)?(?:agent|spending|spend)"
    r"|start\s+(?:the\s+)?x402\s+agent"
    r")\s*(?:[.!]+\s*)?$",
    re.IGNORECASE,
)

# Full shutdown: stops EVERY VOOL process and disables auto-restart until the next manual launch.
# Distinct from the freeze brake -- checked first so "/stopall" is never read as "/stop ...".
_STOPALL_RE = re.compile(
    r"^\s*(?:please\s+)?(?:"
    r"/?stop[\s_-]?all(?:\s+vool)?"
    r"|/?kill[\s_-]?all"
    r"|stop\s+everything"
    r"|kill\s+everything"
    r"|(?:shut\s*down|shutdown|kill|stop)\s+(?:all\s+of\s+)?vool"
    r"|shut\s+everything\s+down"
    r")\s*(?:[.!]+\s*)?$",
    re.IGNORECASE,
)

# The second step of the full shutdown. A /stopall phrase only arms a per-session pending flag;
# this confirm, sent from the same session within the window, is what runs the shutdown -- once.
_STOPALL_CONFIRM_RE = re.compile(
    r"^\s*(?:"
    r"/?stop[\s_-]?all\s+confirm"
    r"|yes[\s,]+(?:please\s+)?(?:shut\s*down|shutdown|kill|stop)\s+(?:all\s+of\s+)?vool"
    r")\s*(?:[.!]+\s*)?$",
    re.IGNORECASE,
)
_STOPALL_CONFIRM_SECONDS = 60.0
_pending_stopall: dict[str, float] = {}
_pending_lock = threading.Lock()


def _now() -> float:
    return time.monotonic()


def _arm_stopall(session_id: str) -> None:
    now = _now()
    with _pending_lock:
        for key, deadline in list(_pending_stopall.items()):
            if deadline < now:
                del _pending_stopall[key]
        _pending_stopall[session_id] = now + _STOPALL_CONFIRM_SECONDS


def _take_stopall(session_id: str) -> bool:
    """Consume this session's pending shutdown. True only if it was armed and has not expired."""
    with _pending_lock:
        deadline = _pending_stopall.pop(session_id, None)
    return deadline is not None and _now() <= deadline


def _response(text: str, *, intent: str, success: bool = True) -> dict[str, Any]:
    return {
        "response": text,
        "confidence": 1.0,
        "source": "vool_agent_brake",
        "deterministic": True,
        "intent": intent,
        "success": success,
    }


def _set_frozen(frozen: bool, wallet_fn: Callable[[], Any] | None) -> bool:
    """Flip and persist the panic-freeze flag. Returns the PRIOR frozen state.

    Raises on any failure to load/persist so the caller never reports a freeze that
    did not actually take effect.
    """
    # ONE mirrored flip: the legacy file is written first and the canonical freeze
    # (core.wallet.limits) last, with rollback -- a failed persist never leaves the two stores
    # disagreeing with what this brake reports. No wallet is loaded.
    from core.wallet_spend_policy_store import set_frozen_mirrored

    wallet = wallet_fn() if wallet_fn is not None else None
    return set_frozen_mirrored(bool(frozen), wallet=wallet)


def _is_windows() -> bool:
    return os.name == "nt"


def _under_bundle_supervisor(vool_home: Any) -> bool:
    """True when this API runs under a live bundle supervisor of ``vool_home``: the supervisor
    sets VOOL_BUNDLE_ROOT for its children and keeps its state file while it runs."""
    if not os.environ.get("VOOL_BUNDLE_ROOT") or vool_home is None:
        return False
    try:
        return (vool_home / "run" / "bundle-supervisor.json").is_file()
    except OSError:
        return False


def _default_stopall() -> bool:
    """Start the full shutdown. Returns True only if something that stops VOOL was started.

    The detached hard-stop is launched first. Under the installed bundle the API lives in the
    supervisor's kill-on-close job, so the supervisor's own stop signal -- the file the Quit VOOL
    button writes -- is also written here, in-process: it stops the window, the API and Ollama
    whether or not a child of this API can leave the job. Without a supervisor nothing would
    read that file, so it is not written and does not count.
    """
    from pathlib import Path

    from core.runtime_paths import active_vool_home
    from installer import vool_stop

    project_root = Path(__file__).resolve().parents[1]
    vool_home = active_vool_home()
    launched = vool_stop.spawn_detached_stop(project_root=project_root, vool_home=vool_home)
    signaled = _under_bundle_supervisor(vool_home) and vool_stop.signal_bundle_supervisor(vool_home)
    return bool(launched or signaled)


def _default_unfreeze_consent(reason: str) -> bool:
    """Live OS-consent prompt for resuming spending (Windows Hello / credential prompt)."""
    from core.os_consent_gate import require_os_user_consent

    return require_os_user_consent(reason)


def maybe_handle_agent_brake(
    user_text: str,
    session_id: str,
    *,
    wallet_fn: Callable[[], Any] | None = None,
    stopall_fn: Callable[[], bool] | None = None,
    consent_fn: Callable[[str], bool] | None = None,
    owner_local: bool = True,
) -> dict[str, Any] | None:
    """Handle /stopall (full shutdown), /stopx402 (freeze) and /startx402 (unfreeze); else None.

    ``session_id`` keys the two-step ``/stopall`` confirm: a /stopall phrase only arms that
    session's pending shutdown, and ``/stopall confirm`` from the same session within
    ``_STOPALL_CONFIRM_SECONDS`` runs it, once. The freeze is global to the wallet, not per
    session, so /stopx402 and /startx402 do not use it and stay one step.

    ``owner_local`` marks a request from the machine owner's own local session (see
    :mod:`core.request_trust`). The two risk-increasing directions are gated on it:
    ``/stopall`` and its confirm (a full daemon shutdown — a remote DoS otherwise) and ``/startx402``
    (unfreeze — re-enabling spend). Freezing (``/stopx402``) is the safe direction and stays
    reachable from anywhere, so an emergency stop is never blocked. Defaults to True for
    trusted in-process callers (CLI); the HTTP dispatcher passes the loopback-derived value.
    """
    text = str(user_text or "")
    stopall_key = str(session_id or "")
    asked = bool(_STOPALL_RE.match(text))
    confirmed = not asked and bool(_STOPALL_CONFIRM_RE.match(text))

    if asked or confirmed:
        if not owner_local:
            return _response(
                "A full VOOL shutdown can only be triggered from your own local session, "
                "not a remote channel or a non-local caller.",
                intent="agent_brake_stopall",
                success=False,
            )
        if asked:
            # Step one runs nothing: it arms this session's confirm and says what would stop.
            _arm_stopall(stopall_key)
            return _response(
                "This stops all of VOOL (the window, the API and the local model runner). "
                f"Type `/stopall confirm` within {int(_STOPALL_CONFIRM_SECONDS)} seconds to go ahead.",
                intent="agent_brake_stopall",
            )
        if not _take_stopall(stopall_key):
            return _response(
                "There is no VOOL shutdown waiting to be confirmed in this chat, so nothing was "
                "stopped. Type `/stopall` first, then `/stopall confirm` within "
                f"{int(_STOPALL_CONFIRM_SECONDS)} seconds.",
                intent="agent_brake_stopall",
                success=False,
            )
        launched = (stopall_fn or _default_stopall)()
        windows = _is_windows()
        if not launched:
            # Windows installs no "Stop VOOL" shortcut (installer/bundle/vool.iss [Icons]); the
            # Quit VOOL button in the sidebar writes the supervisor's stop signal itself.
            fallback = (
                "Use the Quit VOOL button at the bottom of the sidebar to stop everything."
                if windows
                else "Use the 'Stop VOOL' desktop shortcut, or close the VOOL window, to stop everything."
            )
            return _response(
                f"I could not start the shutdown just now. {fallback}",
                intent="agent_brake_stopall",
                success=False,
            )
        if windows:
            # The bundle supervisor's stop signal stops the window, the API and Ollama. Windows
            # has no OpenClaw gateway and no auto-restart task, so neither is named.
            return _response(
                "Shutting down VOOL now: the window, the API and the local model runner (Ollama). "
                "VOOL stays off until you start it again from the Start menu.",
                intent="agent_brake_stopall",
            )
        return _response(
            "Shutting down all of VOOL now -- the API, the OpenClaw gateway, the agent, and the "
            "auto-restart task. The dashboard will go offline in a few seconds and stay down until "
            "you launch VOOL again from the desktop shortcut.",
            intent="agent_brake_stopall",
        )

    if _STOP_RE.match(text):
        try:
            was_frozen = _set_frozen(True, wallet_fn)
        except Exception as exc:
            return _response(
                "I could not freeze the wallet just now, so spending is NOT stopped: "
                f"{exc}. Your spend policy is unchanged. Try again, or run `vool spend-freeze` "
                "from a terminal.",
                intent="agent_brake_stop",
                success=False,
            )
        if was_frozen:
            return _response(
                "x402 spending was already frozen. No payment or .null registration can go "
                "through until you resume. Say /startx402 to resume.",
                intent="agent_brake_stop",
            )
        return _response(
            "x402 agent stopped. Spending is now frozen -- no x402 payment or .null "
            "registration can go through until you resume. Your assistant and local model keep "
            "running. Say /startx402 to resume.",
            intent="agent_brake_stop",
        )

    if _START_RE.match(text):
        # Resuming spending is the risk-increasing direction. It requires BOTH the owner's
        # own local session AND a live OS consent (matching the CLI), while freezing stays
        # open. Fail closed: a non-owner caller, or any decline/unavailable/exception, leaves
        # spending frozen.
        if not owner_local:
            return _response(
                "Resuming spending can only be done from your own local session. Spending "
                "stays frozen. Say /startx402 from your local VOOL session to resume.",
                intent="agent_brake_start",
                success=False,
            )
        consent = consent_fn or _default_unfreeze_consent
        try:
            granted = bool(consent("Unfreeze the VOOL wallet spend policy (re-enable spending)"))
        except Exception as exc:
            return _response(
                "I did not get OS confirmation to resume spending, so it stays frozen: "
                f"{exc}. Approve the Windows Hello prompt, or run `vool spend-unfreeze` from a "
                "terminal, to resume.",
                intent="agent_brake_start",
                success=False,
            )
        if not granted:
            return _response(
                "Resume was declined at the OS prompt, so spending stays frozen. Say /startx402 "
                "and approve the prompt when you want to resume.",
                intent="agent_brake_start",
                success=False,
            )
        try:
            was_frozen = _set_frozen(False, wallet_fn)
        except Exception as exc:
            return _response(
                "I could not change the freeze state just now: "
                f"{exc}. Your spend policy is unchanged. Run `vool spend-unfreeze` from a "
                "terminal if this persists.",
                intent="agent_brake_start",
                success=False,
            )
        if not was_frozen:
            return _response(
                "x402 spending was already active. Per-tx and daily/weekly caps still apply.",
                intent="agent_brake_start",
            )
        return _response(
            "x402 agent resumed. Spending is unfrozen; your per-tx and daily/weekly caps still "
            "apply, and each spend is still gated by an OS approval prompt.",
            intent="agent_brake_start",
        )

    return None
