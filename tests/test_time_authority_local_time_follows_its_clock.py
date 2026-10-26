"""The time authority's local time comes from its own UTC clock.

`TimeAuthority.now_utc` is the one source of the current instant, and a test (or the runtime)
that swaps it expects every reading to follow. `now_for_timezone` read the system clock
directly instead, so the reminder path, which reads local time, ignored a swapped clock: a
reminders test pinned to 24 Oct 2026 started refusing its own reminder once that day passed.
"""
from __future__ import annotations

from datetime import datetime, timezone

from core.time_authority import CLOCK


def test_local_time_is_the_swapped_utc_instant_in_that_zone(monkeypatch) -> None:
    pinned = datetime(2026, 10, 24, 7, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(CLOCK, "now_utc", lambda: pinned)

    local = CLOCK.now_for_timezone("Europe/Athens")

    assert local == pinned, local
    assert local.utcoffset().total_seconds() == 3 * 3600, local  # EEST on that day
    assert str(local.tzinfo) == "Europe/Athens"
