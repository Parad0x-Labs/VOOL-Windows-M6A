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


__all__ = ["checked_folders", "register", "wired"]
