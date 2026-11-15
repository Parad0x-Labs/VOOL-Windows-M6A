"""Local per-capability usage quotas for free vs paid tiers.

Some capabilities cost the operator real money (image/video generation on a rented GPU, paid
API calls). This tracks per-capability usage locally, resets each day, and enforces a per-tier
daily cap *before* the costly call runs, so a free-tier user can't burn the operator's budget.
Paid/operator tiers get higher or unlimited caps. Everything is local: usage counters live under
data_path and never leave the machine; the operator can tune caps via config/quota_limits.json.

Flow: a capability calls `consume_quota("image.generate", tier=active_tier())`; if `allowed` is
False the caller refuses with `remaining`/`day` for a truthful "you've hit today's free limit"
message, and nothing is spent.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import date
from typing import Any

from core import windows_sharing_retry
from core.runtime_paths import config_path, data_path

_USAGE_FILE = "usage_quota.json"
_LIMITS_FILE = "quota_limits.json"  # optional operator override under config/
_KEEP_DAYS = 7

# Built-in per-tier, per-capability daily caps. per_day None = unlimited; "*" = tier default.
_DEFAULT_LIMITS: dict[str, dict[str, dict[str, Any]]] = {
    "free": {
        "image.generate": {"per_day": 3},
        "video.generate": {"per_day": 0},  # paid-only
        "email.send": {"per_day": 20},
        "x.post": {"per_day": 10},
        "*": {"per_day": 50},
    },
    "paid": {
        "image.generate": {"per_day": 200},
        "video.generate": {"per_day": 30},
        "*": {"per_day": None},
    },
    "operator": {"*": {"per_day": None}},  # self / operator = unlimited
}


@dataclass(frozen=True)
class QuotaCheck:
    capability: str
    tier: str
    used: int
    limit: int | None  # None = unlimited
    remaining: int | None  # None = unlimited
    allowed: bool
    day: str


def _today(today: str | None = None) -> str:
    return today or date.today().isoformat()


def active_tier() -> str:
    """The current tier (env VOOL_TIER, default 'free'). Unknown values fall back to 'free'."""
    tier = str(os.environ.get("VOOL_TIER", "") or "").strip().lower()
    return tier if tier in quota_limits() else "free"


def quota_limits() -> dict[str, dict[str, dict[str, Any]]]:
    """Built-in defaults overlaid by an optional operator file (config/quota_limits.json)."""
    limits = {tier: {cap: dict(v) for cap, v in caps.items()} for tier, caps in _DEFAULT_LIMITS.items()}
    try:
        override_path = config_path(_LIMITS_FILE)
        if override_path.exists():
            override = json.loads(override_path.read_text(encoding="utf-8"))
            if isinstance(override, dict):
                for tier, caps in override.items():
                    if isinstance(caps, dict):
                        limits.setdefault(str(tier), {}).update(
                            {str(c): dict(v) for c, v in caps.items() if isinstance(v, dict)}
                        )
    except Exception:
        pass
    return limits


def capability_limit(capability: str, tier: str) -> int | None:
    """Per-day cap for (capability, tier). None = unlimited; falls back to the tier's '*' default."""
    limits = quota_limits()
    tier_caps = limits.get(str(tier)) or limits.get("free") or {}
    entry = tier_caps.get(str(capability))
    if entry is None:
        entry = tier_caps.get("*", {})
    value = entry.get("per_day", None)
    return None if value is None else max(0, int(value))


# One process-wide lock so two callers can't read the same count and both write it back.
_usage_lock = threading.RLock()


def _usage_path():
    return data_path(_USAGE_FILE)


class _UsageRefused(Exception):
    """The usage file exists but the OS refused to open it (still held after the bounded retry)."""


def _load_usage() -> dict[str, dict[str, int]]:
    """Today's counts. A file that cannot be parsed reads as empty, as before; a file the OS refuses to open
    raises ``_UsageRefused``: "nothing used today" there would let one more through and the write-back would
    wipe every other count."""
    path = _usage_path()
    if not path.exists():
        return {}
    try:
        # Windows refuses the open for a moment while another writer's replace swaps the file in.
        text = windows_sharing_retry.read_text(path, encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        raise _UsageRefused() from None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _prune(usage: dict[str, dict[str, int]], current_day: str) -> dict[str, dict[str, int]]:
    days = sorted(usage.keys())
    if len(days) <= _KEEP_DAYS:
        return usage
    keep = set(days[-_KEEP_DAYS:]) | {current_day}
    return {day: counts for day, counts in usage.items() if day in keep}


def _save_usage(usage: dict[str, dict[str, int]]) -> None:
    path = _usage_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write a whole new file and swap it in, so a crash mid-write never leaves a half file that
    # would read back as "nothing used today".
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(usage, sort_keys=True), encoding="utf-8")
    try:
        # Windows refuses the replace for a moment while a reader holds the file; the bounded retry waits it out.
        windows_sharing_retry.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def store_readable() -> bool:
    """False when the usage file exists but can't be read, so a cap that must hold can refuse."""
    path = _usage_path()
    if not path.exists():
        return True
    try:
        return isinstance(json.loads(windows_sharing_retry.read_text(path, encoding="utf-8")), dict)
    except Exception:
        return False


def usage_today(capability: str, *, today: str | None = None) -> int:
    try:
        return int(_load_usage().get(_today(today), {}).get(str(capability), 0))
    except _UsageRefused:
        return 0  # a display count only; check_quota and consume_quota refuse on their own


def check_quota(capability: str, *, tier: str, today: str | None = None) -> QuotaCheck:
    """Non-mutating: can at least one more unit be consumed for (capability, tier) today?"""
    day = _today(today)
    limit = capability_limit(capability, tier)
    try:
        used = int(_load_usage().get(day, {}).get(str(capability), 0))
    except _UsageRefused:
        if limit is None:
            return QuotaCheck(capability, tier, 0, None, None, True, day)
        return QuotaCheck(capability, tier, limit, limit, 0, False, day)  # unknown count: the cap holds
    if limit is None:
        return QuotaCheck(capability, tier, used, None, None, True, day)
    return QuotaCheck(capability, tier, used, limit, max(0, limit - used), used < limit, day)


def consume_quota(capability: str, *, tier: str, units: int = 1, today: str | None = None) -> QuotaCheck:
    """Try to consume `units`. Under the cap -> increment + allowed=True. At/over -> no change, allowed=False."""
    day = _today(today)
    with _usage_lock:
        limit = capability_limit(capability, tier)
        try:
            usage = _load_usage()
        except _UsageRefused:
            if limit is None:
                return QuotaCheck(capability, tier, 0, None, None, True, day)
            return QuotaCheck(capability, tier, limit, limit, 0, False, day)  # unknown count: refuse, write nothing
        used = int(usage.get(day, {}).get(str(capability), 0))
        if limit is None:
            return QuotaCheck(capability, tier, used, None, None, True, day)
        if used >= limit:
            return QuotaCheck(capability, tier, used, limit, 0, False, day)
        new_used = min(limit, used + max(1, int(units)))
        usage.setdefault(day, {})[str(capability)] = new_used
        _save_usage(_prune(usage, day))
    return QuotaCheck(capability, tier, new_used, limit, max(0, limit - new_used), True, day)


def release_quota(capability: str, *, units: int = 1, today: str | None = None) -> None:
    """Give back units consumed for something that provably never happened (refused before it left
    the machine, or refused outright by the provider). Never goes below zero."""
    day = _today(today)
    with _usage_lock:
        try:
            usage = _load_usage()
        except _UsageRefused:
            return  # never overwrite a meter that could not be read
        used = int(usage.get(day, {}).get(str(capability), 0))
        if used <= 0:
            return
        usage.setdefault(day, {})[str(capability)] = max(0, used - max(1, int(units)))
        _save_usage(_prune(usage, day))


def reset_usage() -> None:
    """Clear all tracked usage (admin / tests)."""
    path = _usage_path()
    if path.exists():
        windows_sharing_retry.unlink(path, missing_ok=True)


__all__ = [
    "QuotaCheck",
    "active_tier",
    "capability_limit",
    "check_quota",
    "consume_quota",
    "quota_limits",
    "release_quota",
    "reset_usage",
    "store_readable",
    "usage_today",
]
