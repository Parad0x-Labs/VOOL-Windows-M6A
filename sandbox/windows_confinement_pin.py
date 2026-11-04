"""The pinned VoolConfine launcher folder (docs/WINDOWS_CONFINEMENT.md section 2, "Shipping").

Owned by the shipping chunk. Stub: no launcher is ever verified, so the backend stays unavailable.
"""

from __future__ import annotations

from pathlib import Path


def verified_launcher(project_root: Path) -> Path | None:
    """VoolConfine.exe from the installed `{app}\\broker\\confine\\` or a checkout's
    `windows\\broker\\publish\\confine\\`, only when every file in that folder matches the pin
    manifest and no unlisted file is present. Never from PATH. Stub: always None."""
    return None
