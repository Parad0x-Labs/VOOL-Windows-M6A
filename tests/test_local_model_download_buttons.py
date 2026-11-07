"""Settings and Setup offer the download where a new user meets "no model" (clean-VM job 52, D2).

Job 52: Settings' empty state said "Install one (e.g. `ollama pull qwen3:8b`)" and Setup's no-local
box said only that nothing could answer, so a user without a terminal had no way forward. Both now
show a Download button per offered model, naming its size and where it downloads from (Ollama's
registry, so the consent is informed), with progress and Cancel, through the one pull door.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.vool_settings_page import render_vool_settings_html
from core.vool_setup_page import render_vool_setup_html

_CATALOGS = sorted((Path(__file__).resolve().parents[1] / "core" / "i18n" / "catalogs").glob("*.json"))
_KEYS = ("models.download.button", "models.download.progress", "models.download.cancel",
         "models.download.failed", "models.download.done")


@pytest.mark.parametrize("render", [render_vool_settings_html, render_vool_setup_html], ids=["settings", "setup"])
def test_the_page_downloads_through_the_one_pull_door(render) -> None:
    page = render(build_commit="test-commit")
    assert "'/api/models/local/pull'" in page
    assert "'/api/models/local/pull/cancel'" in page
    for key in _KEYS:
        assert f"'{key}'" in page, key
    assert "Ollama" in page and "registry" in page


def test_settings_no_longer_tells_a_new_user_to_open_a_terminal() -> None:
    assert "ollama pull qwen3:8b" not in render_vool_settings_html(build_commit="test-commit")


@pytest.mark.parametrize("catalog", _CATALOGS, ids=[c.stem for c in _CATALOGS])
def test_every_locale_names_the_download(catalog: Path) -> None:
    messages = json.loads(catalog.read_text(encoding="utf-8"))["messages"]
    for key in _KEYS:
        assert messages.get(key, "").strip(), (catalog.stem, key)
    assert "{model}" in messages["models.download.button"] and "{size}" in messages["models.download.button"]
