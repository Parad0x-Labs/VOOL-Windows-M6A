"""A plain ``vool://`` link opens (or focuses) VOOL; a rejected link says so.

Capability test on 0.7.1-beta (A10): Win+R ``vool://`` did nothing. The registered handler only
accepted the OpenRouter OAuth callback; every other link raised ValueError inside main() and the
handler exited 1 under pythonw with no window and no message. A plain or unknown vool:// link now
starts VOOL the way the Start-menu shortcut does (which brings an open window forward) and takes
nothing from the link. The OAuth callback stays exactly as strict, and a link that is refused, or
a callback that cannot be delivered, tells the user instead of failing silently.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from installer.bundle import vool_protocol_handler as handler


@pytest.fixture()
def wired(monkeypatch, tmp_path):
    seen: dict[str, list] = {"started": [], "posted": [], "notified": []}
    monkeypatch.setattr(handler, "_start_bundle", lambda root: seen["started"].append(root))
    monkeypatch.setattr(handler, "_post_callback", lambda code, state: seen["posted"].append((code, state)) or True)
    monkeypatch.setattr(handler, "_notify", lambda message: seen["notified"].append(message))
    monkeypatch.setattr(handler, "_bundle_root", lambda: tmp_path)
    return seen


@pytest.mark.parametrize(
    "uri",
    (
        "vool://",
        "vool:",
        "VOOL://",
        "vool://open",
        "vool://chat/",
        "vool://run?cmd=calc.exe&args=%2Fc",
        "vool://C:/Windows/System32/cmd.exe",
        "vool://%22%20%26%20calc",
    ),
)
def test_a_plain_or_unknown_vool_link_opens_vool_and_uses_nothing_from_it(wired, tmp_path, uri) -> None:
    assert handler.main([uri]) == 0
    assert wired["started"] == [tmp_path]  # the bundle root only, never anything from the link
    assert wired["posted"] == []
    assert wired["notified"] == []


@pytest.mark.parametrize(
    "uri",
    (
        "vool://auth/openrouter/callback?code=x",
        "vool://auth/openrouter/callback?code=x&state=y&state=z",
        "vool://auth/openrouter/callback?code=x&state=y#fragment",
        "vool://auth/wrong?code=x&state=y",
        "vool://AUTH/other",
    ),
)
def test_a_malformed_auth_link_stays_refused_and_says_so(wired, uri) -> None:
    assert handler.main([uri]) == 1
    assert wired["posted"] == []
    assert wired["started"] == []
    assert len(wired["notified"]) == 1
    assert "code=" not in wired["notified"][0]


@pytest.mark.parametrize(
    "uri",
    ("https://example.com/", "file:///C:/Windows/System32/cmd.exe", "", "vool://" + "x" * 20000),
    ids=("https", "file", "empty", "oversized"),
)
def test_a_link_that_is_not_a_usable_vool_link_is_refused_and_says_so(wired, uri) -> None:
    assert handler.main([uri]) == 1
    assert wired["started"] == []
    assert wired["posted"] == []
    assert len(wired["notified"]) == 1


def test_a_valid_callback_is_still_delivered_without_a_message(wired) -> None:
    assert handler.main(["vool://auth/openrouter/callback?code=abc&state=st"]) == 0
    assert wired["posted"] == [("abc", "st")]
    assert wired["notified"] == []


def test_a_callback_that_cannot_be_delivered_says_so(monkeypatch, wired) -> None:
    monkeypatch.setattr(handler, "deliver_callback", lambda uri, **_kw: False)
    assert handler.main(["vool://auth/openrouter/callback?code=secretcode&state=secretstate"]) == 1
    assert len(wired["notified"]) == 1
    assert "secretcode" not in wired["notified"][0] and "secretstate" not in wired["notified"][0]


def test_vool_that_cannot_be_started_says_so(monkeypatch, wired) -> None:
    def _missing(_root: Path) -> None:
        raise FileNotFoundError("VOOL.cmd is missing")

    monkeypatch.setattr(handler, "_start_bundle", _missing)
    assert handler.main(["vool://"]) == 1
    assert len(wired["notified"]) == 1
