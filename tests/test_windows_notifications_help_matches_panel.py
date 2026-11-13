"""Windows: the Notifications row says only what the Windows panel offers.

On Windows the panel has no OS notification bridge (``bell_only_windows``), so ``paintMac`` stops at
``native.supported === false`` and the panel never shows "Send a test notification". The Windows help
text still said "send a test alert", in every language. It now names only what the panel has (quiet
hours and sound), in all 17 catalogs. macOS keeps its own text.
"""
from __future__ import annotations

import json

import pytest

from core.i18n.catalog import CATALOGS_DIR
from core.notification_settings_fragment import _JS

KEY = "settings.row.notifications_panel.help.windows"

#: The "send a test alert" clause as each catalog spelled it.
TEST_ALERT_CLAUSE = {
    "ar": "تنبيه اختبار", "de": "Testerinnerung", "en": "test alert", "es": "alerta de prueba",
    "fr": "alerte de test", "he": "התראת בדיקה", "hi": "परीक्षण अलर्ट", "ja": "テスト", "ko": "시험 알림",
    "lt": "bandomąjį", "pl": "alert testowy", "pt": "alerta de teste", "ru": "тестовое", "tr": "sınama uyarısı",
    "uk": "тестове", "vi": "nhắc thử", "zh-Hans": "测试",
}
LOCALES = sorted(path.stem for path in CATALOGS_DIR.glob("*.json"))


def test_every_catalog_is_listed() -> None:
    assert sorted(TEST_ALERT_CLAUSE) == LOCALES


@pytest.mark.parametrize("tag", LOCALES)
def test_the_windows_help_does_not_promise_a_test_alert(tag) -> None:
    messages = json.loads((CATALOGS_DIR / f"{tag}.json").read_text(encoding="utf-8"))["messages"]
    assert TEST_ALERT_CLAUSE[tag] not in messages[KEY], f"{tag}: {messages[KEY]!r}"
    assert "Windows" in messages[KEY]


def test_the_windows_panel_really_has_no_test_button() -> None:
    paint_mac = _JS[_JS.index("function paintMac(){"):_JS.index("function paintPrefs(){")]
    stop = paint_mac.index("if (native.supported === false) return;")
    assert paint_mac.index("vns.btn.test") > stop
    assert "vns.btn.test" not in _JS[_JS.index("function paintPrefs(){"):]
