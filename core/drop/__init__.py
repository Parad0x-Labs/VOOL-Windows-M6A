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


#: Drop guards its folders with POSIX mode bits, the owner's uid and flock, none of which hold on Windows
#: (K4: a mode-bit check never counts as a pass there), so on Windows it refuses before touching anything.
_IS_WINDOWS = os.name == "nt"


def require_enabled():
    if not enabled():
        raise DropError("DROP_DISABLED")
    if _IS_WINDOWS:
        raise DropError("DROP_NOT_ON_WINDOWS")
