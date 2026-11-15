"""On Windows the weather lane's coordinate check reads the zone table from the `tzdata` package.

Windows ships no system tz database, so `zoneinfo.TZPATH` names no readable zone.tab and the
city-coordinate table was empty there: a provider report from half a world away ("Talinn" geolocated
to Bouma'chouk, Algeria; "Paris" answered from Paris, Texas) had nothing to be compared with and was
accepted on its name. These tests stand in a Windows host: no TZPATH, `sys.platform == "win32"`, and a
`tzdata` package whose `zoneinfo/zone.tab` holds the two real cities.
"""

from __future__ import annotations

import sys
import types
from unittest import mock

import pytest

from tests.test_a_failed_weather_lookup_never_fabricates_a_city import _always_returns, _wttr_payload
from tools.web import web_research


@pytest.fixture(autouse=True)
def _web_lookups_on(monkeypatch):
    """These rows exercise the live lookups themselves, so Settings > Web lookups is on for them. Since 0.7.2 the
    lookups refuse while it is off (tests/test_web_lookups_off_makes_no_live_fetch.py holds those rows)."""
    from core import policy_engine

    monkeypatch.setattr(policy_engine, "allow_web_fallback", lambda: True)


_ZONE_TAB = "# tzdb zone.tab (two real rows)\nEE\t+5925+02445\tEurope/Tallinn\nFR\t+4852+00220\tEurope/Paris\n"


@pytest.fixture
def windows_host_with_tzdata(tmp_path, monkeypatch):
    package = tmp_path / "tzdata"
    (package / "zoneinfo").mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "zoneinfo" / "zone.tab").write_text(_ZONE_TAB, encoding="utf-8")
    fake = types.ModuleType("tzdata")
    fake.__file__ = str(package / "__init__.py")
    monkeypatch.setitem(sys.modules, "tzdata", fake)
    monkeypatch.setattr("zoneinfo.TZPATH", ())
    monkeypatch.setattr(sys, "platform", "win32")
    web_research._tzdb_city_table.cache_clear()
    yield
    web_research._tzdb_city_table.cache_clear()


def test_the_zone_table_comes_from_the_tzdata_package(windows_host_with_tzdata) -> None:
    table = web_research._tzdb_city_table()
    assert set(table) == {"tallinn", "paris"}
    assert table["tallinn"] == pytest.approx((59.4167, 24.75), abs=0.01)


def test_the_talinn_mis_geolocation_still_declines_on_windows(windows_host_with_tzdata) -> None:
    with mock.patch.object(
        web_research,
        "_open_remote",
        side_effect=_always_returns(
            _wttr_payload("Bouma'chouk", "Algeria", region="Tipaza", lat="36.600", lon="2.417")
        ),
    ):
        assert web_research.structured_weather_lookup("Talinn") is None


def test_paris_texas_does_not_answer_for_paris_on_windows(windows_host_with_tzdata) -> None:
    with mock.patch.object(
        web_research,
        "_open_remote",
        side_effect=_always_returns(_wttr_payload("Paris", "United States of America", lat="33.662", lon="-95.555")),
    ):
        assert web_research.structured_weather_lookup("Paris") is None


def test_the_real_city_still_answers_on_windows(windows_host_with_tzdata) -> None:
    with mock.patch.object(
        web_research,
        "_open_remote",
        side_effect=_always_returns(_wttr_payload("Paris", "France", lat="48.857", lon="2.352")),
    ):
        assert web_research.structured_weather_lookup("Paris") is not None
