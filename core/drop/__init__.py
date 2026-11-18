"""VOOL Drop: private VOOL-to-VOOL handoffs. Off by default.

UNREVIEWED: not for real messages. The DROP-HYBRID-1 session protocol has had no
independent cryptographic review; keep ``VOOL_DROP`` unset outside testing.
"""

import os

from core.drop.session import DropError

WARNING = "UNREVIEWED: not for real messages"

__all__ = ["WARNING", "DropError", "enabled", "require_enabled"]


def enabled() -> bool:
    return os.environ.get("VOOL_DROP") == "1"


def require_enabled():
    # On Windows Drop judges its folders by their ACL, not mode bits, and writes outside VOOL's own home only
    # inside a folder the owner marked disposable (core/drop/service.py, core/drop/store.py).
    if not enabled():
        raise DropError("DROP_DISABLED")
