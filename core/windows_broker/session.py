"""One list of flagged folders for the Windows gate and the broker (docs/WINDOWS_BROKER.md section 4).

Once the API has started, or tried to start, the broker (`process.start_for_api`), the gate takes
its flagged folders from the broker's start line instead of re-reading the variable, and only those
whose current final path, asked of the broker (`flagged_path`, answered from the handle it holds),
is still the one it reported. A broker that is not running, or cannot be asked, marks no folder,
so every gated call is refused. Unknown states fail closed.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING, Any

from .client import BrokerClient
from .protocol import BrokerRefusal, EffectOutcomeUnknown

if TYPE_CHECKING:
    from .process import BrokerHost, BrokerStatus

logger = logging.getLogger(__name__)

_lock = threading.Lock()
#: The API's broker host, once start_for_api has made one; None in any other process.
_host: BrokerHost | None = None


def register(host: BrokerHost) -> None:
    global _host
    with _lock:
        _host = host


def wired() -> bool:
    """Whether this process started (or tried to start) a broker, so the gate must use its list."""
    return _host is not None


def _client_for(status: BrokerStatus, secret: bytes) -> Any:
    return BrokerClient(status.pipe, secret, status.pid)


def checked_folders() -> list[str]:
    """The broker's flagged folders whose final path is still the one it reported at start.

    Empty when no broker runs or it cannot be asked: the gate then marks no folder."""
    host = _host
    if host is None:
        return []
    status = host.status
    if not status.running or not status.flagged:
        return []
    client = _client_for(status, host.secret)
    folders: list[str] = []
    try:
        client.connect()
        for folder in status.flagged:
            try:
                answer = client.request("flagged_path", flagged_id=folder.id)
            except BrokerRefusal as refusal:
                logger.warning("Flagged folder %s is not marked: the broker refused flagged_path (%s)", folder.path, refusal)
                continue
            path = answer.get("path")
            if not isinstance(path, str) or os.path.normcase(path) != os.path.normcase(folder.path):
                logger.warning("Flagged folder %s is not marked: its path is now %r", folder.path, path)
                continue
            folders.append(folder.path)
    except (ConnectionError, EffectOutcomeUnknown, OSError, ValueError) as exc:
        logger.warning("No flagged folder is marked: the broker could not be asked (%s)", exc)
        return []
    finally:
        client.close()
    return folders


def _pending_row(row: Any) -> dict[str, Any]:
    if isinstance(row, dict) and isinstance(row.get("batch_id"), str) and row["batch_id"]:
        left = row.get("left")
        return {"batch_id": row["batch_id"], "left": [str(item) for item in left] if isinstance(left, list) else []}
    return {"batch_id": "unknown", "left": []}


def _torn_row(row: Any) -> dict[str, Any]:
    if isinstance(row, dict) and isinstance(row.get("batch_id"), str) and row["batch_id"]:
        size, kept_in = row.get("bytes"), row.get("kept_in")
        return {"batch_id": row["batch_id"],
                "bytes": size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else 0,
                "kept_in": kept_in if isinstance(kept_in, str) else ""}
    return {"batch_id": "unknown", "bytes": 0, "kept_in": ""}


def report() -> dict[str, Any]:
    """What VOOL can show about the broker (docs/WINDOWS_BROKER.md section 8), as one plain dict.

    ``{"wired": False}`` in a process that never started a broker. Otherwise whether it runs (and
    why not), the flagged and skipped entries, whether undo records can be written, the batches
    still pending and the torn last records. A journal state that is missing or malformed reads as
    unavailable; a row that is malformed is kept as an ``unknown`` row so it still needs a check."""
    host = _host
    if host is None:
        return {"wired": False}
    status = host.status
    journal = status.journal if status.running and isinstance(status.journal, dict) else {}
    pending, torn = journal.get("pending"), journal.get("torn")
    pending_rows = [_pending_row(row) for row in pending] if isinstance(pending, list) else []
    torn_rows = [_torn_row(row) for row in torn] if isinstance(torn, list) else []
    return {
        "wired": True,
        "running": bool(status.running),
        "reason": status.reason,
        "flagged": [{"id": folder.id, "path": folder.path} for folder in status.flagged],
        "skipped": [dict(item) for item in status.skipped],
        # Both row lists must be there: a journal state without them is malformed, never healthy.
        "undo_records": ("available" if journal.get("available") is True and isinstance(pending, list)
                         and isinstance(torn, list) else "unavailable"),
        "pending": pending_rows,
        "torn": torn_rows,
        "needs_check": bool(pending_rows or torn_rows),
    }


__all__ = ["checked_folders", "register", "report", "wired"]
