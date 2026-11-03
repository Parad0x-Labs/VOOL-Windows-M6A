"""The clock lane's city table must be built from a database this interpreter can read.

Windows baseline, head d6866a4 (70 red node ids over slices 02b/04/05/06): the city -> zone
table was scanned only from `zoneinfo.TZPATH`, which on Windows names no readable directory, so
the table was `{}` while `ZoneInfo("Asia/Tokyo")` resolved fine through the pip `tzdata`
package -- two zone sources on one interpreter, one of them empty. Every named-place clock turn
then declined, or was answered in this machine's own zone:

    what time is it in 5h from now in Tokyo? Output exactly one word. -> None  (s05-r1 :69)
    in 5 hours in tokyo what time is it
      -> "Current time is 15:23 Pacific Daylight Time; in 5 hours it will be
          20:23 Pacific Daylight Time."                                       (s05-r1 :107)
    What time is it in Tokyo right now? -> date_time_fast_path returned None  (s06-b12 :82)

The property that failed is the AGREEMENT: wherever `ZoneInfo` can read a zone, the table the
clock lane resolves places with must resolve that zone's city. That is what this file pins, on
every platform, rather than any particular directory.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from core.agent_runtime.fast_paths_utility import _canonical_zone_leaves, date_time_fast_path

#: 2026-08-14 22:23 UTC -- the instant the reported five-hours turn was measured against.
PIN = datetime(2026, 8, 14, 22, 23, tzinfo=timezone.utc)


def test_wherever_zoneinfo_reads_a_zone_the_city_table_resolves_its_city() -> None:
    """`ZoneInfo` resolving while the table is empty is exactly the split that broke Windows."""

    assert ZoneInfo("Asia/Tokyo") is not None  # a zone database exists on this host in SOME form

    leaves = _canonical_zone_leaves()

    assert leaves.get("tokyo") == "Asia/Tokyo", "the clock lane's table is empty or incomplete"


def test_a_mid_sentence_place_is_answered_in_its_own_zone_not_this_machines() -> None:
    """End to end: a place named inside the sentence binds the answer to that city.

    This wording is the one that did NOT fail closed on the empty table -- the place is not
    phrase-final, so the unresolvable-place gate missed it and this machine's own zone answered.
    """

    answer = date_time_fast_path(
        None, "in 5 hours in tokyo what time is it", source_surface="api", now_utc=PIN
    )

    assert answer is not None
    assert "Tokyo" in answer, answer
    assert "12:23" in answer, answer
