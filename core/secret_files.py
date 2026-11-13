"""Windows: VOOL's known secret files are made owner-only once at startup.

Each secret writer seals its file at write time (`core.owner_only.write_private_file`), and each reader
repairs a file written before that the first time it reads it. A file VOOL never reads in a session
was never repaired: on Steve (Windows 11, round 18, 12 Nov 2026) voolbook_token.secret, written 11 Nov
and never read without a Voolbook sign-in, still carried CodexSandboxUsers:(RX).

`repair_known_secret_files` runs `repair_private_file` once over every known secret file that exists.
It never raises and never creates a file. Off Windows (`owner_only.seals_secrets()` False) it does
nothing at its first statement.
"""
from __future__ import annotations

import logging
from pathlib import Path

from core import owner_only

logger = logging.getLogger(__name__)


def known_secret_files() -> list[Path]:
    """The secret files VOOL writes, where this install keeps them. Nothing is created."""
    from core.runtime_paths import data_path

    paths = [
        data_path("credentials.enc.json"),
        data_path("credentials.meta.json"),
        data_path("voolbook_token.secret"),
        data_path("a8_digest_key.hex"),
        data_path("owner_identity.json"),
    ]
    try:
        from core.vool_wallet import VoolWallet

        paths.append(VoolWallet().wallet_path)  # the legacy wallet file; only its path is computed
    except Exception:
        pass
    try:
        from core.wallet_spend_policy_store import policy_path

        paths.append(policy_path())  # written through the wallet's private writer
    except Exception:
        pass
    try:
        from core.blackbox.store import store_root

        paths.append(Path(store_root()) / "journal.key")
    except Exception:
        paths.append(data_path("blackbox", "journal.key"))
    return paths


def repair_known_secret_files() -> dict[str, str]:
    """Make each existing known secret file owner-only. Returns {name: reason} for each that could not
    be; empty when every one is sealed (or off Windows). Never raises."""
    if not owner_only.seals_secrets():
        return {}
    failed: dict[str, str] = {}
    try:
        paths = known_secret_files()
    except Exception:
        logger.exception("could not list VOOL's secret files for the startup seal")
        return {"*": "list_failed"}
    for path in paths:
        try:
            report = owner_only.repair_private_file(path)
        except Exception:  # repair_private_file never raises; this keeps boot safe if that changes
            report = owner_only.OwnerOnlyReport(False, "repair_failed")
        if not report.ok:
            failed[path.name] = str(report.reason or "not_owner_only")
    return failed


__all__ = ["known_secret_files", "repair_known_secret_files"]
