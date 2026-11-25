"""Every wallet refusal is filed once, typed, in the runtime's fault plane, then raised."""
from __future__ import annotations

import uuid
from typing import Any

from core.faults.recorder import identity_from_context, record_fault
from core.faults.records import FaultRecord
from core.wallet.errors import WalletFault
from core.wallet.redaction import redact_wallet_record


def wallet_fault(code: str, *, authority: str, context: dict[str, Any] | None = None, source_context: dict[str, Any] | None = None, dedupe: str = "") -> WalletFault:
    """Record a typed fault (security observation included when the catalog says so) and return the exception to raise."""
    clean_context = redact_wallet_record(dict(context or {}))
    turn_key, session_id = identity_from_context(source_context)
    fault_id = ""
    message = ""
    try:
        record = FaultRecord.for_code(
            code,
            authority=authority,
            turn_key=turn_key,
            session_id=session_id,
            dedupe=dedupe or f"wallet-{uuid.uuid4().hex}",
            context={k: str(v) for k, v in clean_context.items()},
        )
        message = record.user_message
        fault_id = record_fault(record)
    except Exception:
        fault_id = ""
    if code == "wallet_limit_exceeded" and str(clean_context.get("limit") or "") == "frozen" and _windows():
        # The panic freeze refuses through the limit fault (limits.LIMIT_FROZEN), whose catalog sentence says the payment
        # is above a spending limit. Steve row 38 (0.7.3 at 8987cac7): "send Testy 1 USDT" with the freeze on was told
        # exactly that, with every limit far above 1 USDT. On Windows the person is told the freeze is why.
        message = FROZEN_MESSAGE
    return WalletFault(code, context=clean_context, fault_id=fault_id, message=message)


FROZEN_MESSAGE = "Payments are frozen (panic freeze is on), so it was not sent. Type /unfreeze to lift it."


def _windows() -> bool:
    try:
        from core import windows_write_gate

        return windows_write_gate.active()
    except Exception:
        return False
