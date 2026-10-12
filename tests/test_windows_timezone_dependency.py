"""Windows must ship the timezone data used by clocks and reminder scheduling."""
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPENDENCY = "tzdata==2026.5"


def test_windows_timezone_data_is_declared_and_bundled():
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib

    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert DEPENDENCY in metadata["project"]["dependencies"]
    assert f'"{DEPENDENCY}"' in (ROOT / "installer/bundle/build_bundle.ps1").read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ["UTC", "Asia/Tokyo", "Europe/Paris", "America/Vancouver"])
def test_windows_runtime_can_load_named_timezones(name):
    assert ZoneInfo(name).key == name


@pytest.fixture
def package_zone_tables(monkeypatch):
    from core.agent_runtime import fast_paths_utility

    monkeypatch.setattr(fast_paths_utility.zoneinfo, "TZPATH", ())
    fast_paths_utility._canonical_zone_leaves.cache_clear()
    try:
        yield fast_paths_utility
    finally:
        fast_paths_utility._canonical_zone_leaves.cache_clear()


def test_windows_city_resolver_reads_bundled_canonical_tables(package_zone_tables):
    leaves = package_zone_tables._canonical_zone_leaves()
    assert leaves["tokyo"] == "Asia/Tokyo"
    assert leaves["paris"] == "Europe/Paris"
    for trap in ("west", "central", "pacific", "mountain", "factory", "universal"):
        assert trap not in leaves


def test_windows_city_clock_uses_the_bundled_resolver(package_zone_tables):
    from datetime import datetime, timezone

    answer = package_zone_tables.date_time_fast_path(
        None, "What time is it in Tokyo?", source_surface="api",
        now_utc=datetime(2026, 10, 10, 0, 0, tzinfo=timezone.utc),
    )
    assert answer == "Current time in Tokyo is 09:00 JST."


def test_absent_timezone_tables_still_decline_named_city(package_zone_tables, monkeypatch):
    def missing_package(name):
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(package_zone_tables.resources, "files", missing_package)
    assert package_zone_tables._canonical_zone_leaves() == {}
    assert package_zone_tables.date_time_fast_path(
        None, "What time is it in Tokyo?", source_surface="api",
    ) is None


def test_system_canonical_tables_keep_priority(package_zone_tables, monkeypatch, tmp_path):
    (tmp_path / "zone.tab").write_text("AA\t+0000+00000\tAsia/Tokyo\n", encoding="utf-8")
    monkeypatch.setattr(package_zone_tables.zoneinfo, "TZPATH", (str(tmp_path),))
    assert package_zone_tables._canonical_zone_leaves() == {"tokyo": "Asia/Tokyo"}
