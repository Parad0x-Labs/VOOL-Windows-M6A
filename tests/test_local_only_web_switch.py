"""The owner's "Block web search in Local Only" switch (Settings -> Privacy & Permissions).

Owner decision, 12 Nov 2026: Local Only keeps web search through VOOL's own browser on every
platform, and the owner gets one switch to turn that off too. These rows pin:

* the switch is persisted by the Settings prefs authority and read back the way the gate reads it;
* with Local Only on and the switch on, every web lane is refused by the ONE decision in
  `core.remote_fetch_policy` (browser doors, web search, SearXNG, the HTTP door's wording, VOOL
  browser sessions), with a sentence that names the switch;
* a switch that cannot be read refuses the web while Local Only is on, and says why;
* switch off keeps today's behaviour, and outside Local Only the switch changes nothing;
* the model cannot flip it: no model lane, no chat sentence, no turn context.

Nothing here opens a socket: every name lookup fails and no browser process is started.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from core import auto_local_only_mode, effect_gateway, policy_engine, user_preferences, windows_write_gate
from core import remote_fetch_policy as rfp
from tools.browser import browser_render as br

_SWITCH = "local_only_block_web"
_LOCAL_ONLY = {"local_only_mode": True}


@pytest.fixture()
def prefs_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "user_preferences.json"
    monkeypatch.setattr(user_preferences, "_prefs_path", lambda: path)
    return path


@pytest.fixture()
def offline(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """No name resolves and no process starts, whatever a check forgot."""

    def _no_dns(*_args, **_kwargs):
        raise socket.gaierror("offline test")

    monkeypatch.setattr(socket, "getaddrinfo", _no_dns)
    launches: list[list[str]] = []

    def _spy(argv, *_args, **_kwargs):
        launches.append(list(argv))
        raise OSError("spy: no process is started in this test")

    monkeypatch.setattr(subprocess, "Popen", _spy)
    monkeypatch.setattr(windows_write_gate, "_IS_WINDOWS", False)
    monkeypatch.setattr(policy_engine, "local_only_mode", lambda: False)
    return launches


def _set(prefs_file: Path, value: Any) -> None:
    prefs_file.write_text(json.dumps({_SWITCH: value, "show_workflow": False}), encoding="utf-8")


def _door(body: dict) -> int:
    from core.web.api.registry_authorities import set_prefs_authority
    from core.web.api.runtime import RuntimeServices

    return set_prefs_authority(body, {"Content-Type": "application/json"}, RuntimeServices(display_name="VOOL")).status


def _listed() -> dict:
    from core.command_registry.groups.convergence import _handle_prefs_list

    return _handle_prefs_list(None, None).data


def _render_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    rendered: list[str] = []
    monkeypatch.setattr(
        br, "_chrome_render_unchecked", lambda url, **_kw: rendered.append(url) or {"status": "ok", "final_url": url}
    )
    return rendered


def _denials(context: dict) -> list[dict]:
    rows = context.get(effect_gateway.EFFECT_RECEIPTS_CONTEXT_KEY) or []
    return [dict(row) for row in rows if row.get("decision") == "denied"]


# ---- persisted, owner-only door -----------------------------------------------------------------


def test_the_switch_is_off_by_default_and_for_an_older_preferences_file(prefs_file: Path) -> None:
    assert not prefs_file.exists()
    assert user_preferences.local_only_block_web_state() is False
    assert user_preferences.load_preferences().local_only_block_web is False
    prefs_file.write_text(json.dumps({"show_workflow": True}), encoding="utf-8")
    assert user_preferences.local_only_block_web_state() is False
    assert _listed()[_SWITCH] is False


def test_the_settings_door_persists_the_switch_and_reads_it_back(prefs_file: Path) -> None:
    assert _door({_SWITCH: True}) == 200
    assert json.loads(prefs_file.read_text(encoding="utf-8"))[_SWITCH] is True
    assert user_preferences.local_only_block_web_state() is True
    assert _listed()[_SWITCH] is True
    # another preference saved later keeps the switch
    assert _door({"show_workflow": True}) == 200
    assert user_preferences.local_only_block_web_state() is True
    assert _door({_SWITCH: False}) == 200
    assert user_preferences.local_only_block_web_state() is False
    assert _listed()[_SWITCH] is False


def test_the_settings_door_refuses_a_switch_value_that_is_not_a_boolean(prefs_file: Path) -> None:
    assert _door({_SWITCH: True}) == 200
    assert _door({_SWITCH: "off"}) == 400
    assert _door({_SWITCH: 0}) == 400
    assert user_preferences.local_only_block_web_state() is True


@pytest.mark.parametrize("content", ["{not json", "[]", json.dumps({_SWITCH: "yes"}), json.dumps({_SWITCH: 1})])
def test_an_unreadable_switch_is_unknown_and_settings_shows_it_on(prefs_file: Path, content: str) -> None:
    prefs_file.write_text(content, encoding="utf-8")
    assert user_preferences.local_only_block_web_state() is None
    assert rfp.local_only_web_block_setting() is None
    # Settings shows what the gate does: it refuses, so the toggle reads ON until it is saved again.
    assert _listed()[_SWITCH] is True


def test_settings_page_has_the_toggle_under_privacy_and_permissions() -> None:
    from core.vool_settings_page import PREFS, settings_groups

    groups = {group["id"]: group for group in settings_groups()}
    rows = {row["id"]: row for row in groups["permissions"]["rows"]}
    row = rows["local_only_block_web"]
    assert row["label"] == "Block web search in Local Only"
    assert row["kind"] == "toggle"
    assert row["read"] == {"url": PREFS, "field": _SWITCH}
    assert row["write"] == {"url": PREFS, "field": _SWITCH, "type": "bool"}
    help_text = row["help"]
    assert help_text.startswith("Off by default.")
    for words in ("web searches", "opening or summarizing a web page", "browser sessions", "changes nothing outside Local Only"):
        assert words in help_text, words


def test_every_refusal_names_the_switch_as_settings_labels_it() -> None:
    from core.vool_settings_page import settings_groups

    group = next(g for g in settings_groups() if g["id"] == "permissions")
    label = next(r["label"] for r in group["rows"] if r["id"] == "local_only_block_web")
    switch = f"Settings → {group['title']} → {label}"
    assert switch == rfp.LOCAL_ONLY_WEB_SWITCH
    for text in (
        rfp.LOCAL_ONLY_WEB_BLOCKED_DENIAL,
        rfp.LOCAL_ONLY_WEB_UNKNOWN_DENIAL,
        rfp.LOCAL_ONLY_EGRESS_DENIAL_WEB_BLOCKED,
        rfp.LOCAL_ONLY_EGRESS_DENIAL_WEB_UNKNOWN,
    ):
        assert switch in text
    assert "turn that setting off" in rfp.LOCAL_ONLY_WEB_BLOCKED_DENIAL
    assert "could not read" in rfp.LOCAL_ONLY_WEB_UNKNOWN_DENIAL


# ---- ON + Local Only: each lane refused at the gate ---------------------------------------------


@pytest.mark.parametrize("windows", [False, True])
def test_on_the_browser_gate_refuses_a_public_address_and_leaves_a_receipt(
    prefs_file: Path, offline: list, monkeypatch: pytest.MonkeyPatch, windows: bool
) -> None:
    _set(prefs_file, True)
    monkeypatch.setattr(windows_write_gate, "_IS_WINDOWS", windows)
    context = dict(_LOCAL_ONLY)
    with rfp.remote_fetch_policy_scope(context):
        assert rfp.browser_navigation_refusal("https://duckduckgo.com/?q=x") == rfp.LOCAL_ONLY_WEB_BLOCKED_DENIAL
    denied = _denials(context)
    assert [row["decided_by"] for row in denied] == ["remote_fetch_policy.local_only_web_switch"]
    assert denied[0]["host"] == "duckduckgo.com"


@pytest.mark.parametrize(
    "local_only",
    [
        {"local_only_mode": True},
        {auto_local_only_mode.CONTEXT_KEY: True},
        "machine",
        "ambient",
    ],
)
def test_on_every_source_of_local_only_binds_the_gate(
    prefs_file: Path, offline: list, monkeypatch: pytest.MonkeyPatch, local_only: Any
) -> None:
    _set(prefs_file, True)
    rendered = _render_spy(monkeypatch)
    context: dict = local_only if isinstance(local_only, dict) else {}
    if local_only == "machine":
        monkeypatch.setattr(policy_engine, "local_only_mode", lambda: True)
    if local_only == "ambient":
        with auto_local_only_mode.local_only_egress_scope(), rfp.remote_fetch_policy_scope(context):
            outcome = br.chrome_render("https://duckduckgo.com/?q=x")
    else:
        with rfp.remote_fetch_policy_scope(context):
            outcome = br.chrome_render("https://duckduckgo.com/?q=x")
    assert outcome["status"] == "egress_refused"
    assert outcome["error"] == rfp.LOCAL_ONLY_WEB_BLOCKED_DENIAL
    assert rendered == []


def test_on_both_browser_doors_refuse_before_any_browser_starts(
    prefs_file: Path, offline: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set(prefs_file, True)
    rendered = _render_spy(monkeypatch)
    monkeypatch.setenv("PLAYWRIGHT_ENABLED", "1")
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    with rfp.remote_fetch_policy_scope(dict(_LOCAL_ONLY)):
        chrome = br.chrome_render("https://example.com/a")
        page = br.browser_render("https://example.com/b")  # the summarize-URL render door
    assert chrome["status"] == page["status"] == "egress_refused"
    assert chrome["error"] == page["error"] == rfp.LOCAL_ONLY_WEB_BLOCKED_DENIAL
    assert rendered == []
    assert offline == []


def test_on_browser_search_reaches_no_search_engine(prefs_file: Path, offline: list, monkeypatch: pytest.MonkeyPatch) -> None:
    from tools.web.browser_search import browser_search

    _set(prefs_file, True)
    rendered = _render_spy(monkeypatch)
    with rfp.remote_fetch_policy_scope(dict(_LOCAL_ONLY)):
        assert browser_search("latest news about jupiter") == []
    assert rendered == []


def test_on_web_research_runs_no_provider_and_says_why(prefs_file: Path, offline: list, monkeypatch: pytest.MonkeyPatch) -> None:
    from tools.web import web_research

    _set(prefs_file, True)
    rendered = _render_spy(monkeypatch)
    monkeypatch.setattr(
        web_research, "_configured_provider_order", lambda: pytest.fail("a search provider was consulted")
    )
    with rfp.remote_fetch_policy_scope(dict(_LOCAL_ONLY)):
        result = web_research.web_research("latest news about jupiter", total_budget_s=30.0)
    assert result.provider == "disabled"
    assert result.hits == [] and result.pages == []
    assert result.notes == ["local_only_web_blocked", rfp.LOCAL_ONLY_WEB_BLOCKED_DENIAL]
    assert rendered == []


def test_on_a_searxng_on_this_machine_is_refused_too(prefs_file: Path, offline: list, monkeypatch: pytest.MonkeyPatch) -> None:
    # Loopback is local to the HTTP door, but SearXNG relays the query to the search engines.
    from tools.web import searxng_client

    _set(prefs_file, True)
    monkeypatch.setattr(searxng_client, "open_remote", lambda *_a, **_k: pytest.fail("the SearXNG request was sent"))
    client = searxng_client.SearXNGClient(base_url="http://127.0.0.1:8080")
    with rfp.remote_fetch_policy_scope(dict(_LOCAL_ONLY)), pytest.raises(rfp.RemoteFetchRefusedError) as refused:
        client.search("latest news about jupiter")
    assert str(refused.value) == rfp.LOCAL_ONLY_WEB_BLOCKED_DENIAL


def test_on_the_http_door_says_web_search_is_off_too(prefs_file: Path, offline: list) -> None:
    _set(prefs_file, True)
    context = dict(_LOCAL_ONLY)
    with rfp.remote_fetch_policy_scope(context), pytest.raises(rfp.RemoteFetchRefusedError) as refused:
        rfp.open_remote(urllib.request.Request("https://example.com/page"), timeout=5)
    assert str(refused.value) == rfp.LOCAL_ONLY_EGRESS_DENIAL_WEB_BLOCKED
    assert "still works" not in str(refused.value)
    assert [row["decided_by"] for row in _denials(context)] == ["remote_fetch_policy.local_only_egress"]


def test_on_an_address_on_this_machine_stays_open(prefs_file: Path, offline: list, monkeypatch: pytest.MonkeyPatch) -> None:
    _set(prefs_file, True)
    rendered = _render_spy(monkeypatch)
    with rfp.remote_fetch_policy_scope(dict(_LOCAL_ONLY)):
        assert rfp.local_only_web_refusal("http://127.0.0.1:8000/page") is None
        assert rfp.local_only_web_refusal("http://192.168.1.20/page") is None
        assert br.chrome_render("http://localhost:8000/page")["status"] == "ok"
    assert rendered == ["http://localhost:8000/page"]


class _FakeHandle:
    def __init__(self, current_url: str = "") -> None:
        self.current_url = current_url
        self.primary_origin = "https://example.com" if current_url else ""
        self.state = "ready"
        self.recently_blocked: list[str] = []
        self.spent = 0
        self.submitted = 0

    def spend_op(self) -> None:
        self.spent += 1

    def submit(self, *_args, **_kwargs):
        self.submitted += 1
        raise AssertionError("the engine was driven")


def _vool_browser(monkeypatch: pytest.MonkeyPatch, handle: _FakeHandle) -> Any:
    from core.vool_browser import api

    monkeypatch.setattr(api, "_require_session", lambda _arguments: handle)
    monkeypatch.setattr(api, "record_receipt", lambda _handle, **kw: dict(kw))
    monkeypatch.setattr(api, "open_session", lambda **_kw: pytest.fail("a browser session was started"))
    return api


def test_on_a_vool_browser_session_cannot_open_on_a_public_page(
    prefs_file: Path, offline: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set(prefs_file, True)
    api = _vool_browser(monkeypatch, _FakeHandle())
    with rfp.remote_fetch_policy_scope(dict(_LOCAL_ONLY)):
        result = api.handle_intent(
            "vool-browser.session.open", {"session": "s1", "start_url": "https://shop.example.com/"}
        )
    assert (result.ok, result.status, result.response_text) == (False, "refused", rfp.LOCAL_ONLY_WEB_BLOCKED_DENIAL)


@pytest.mark.parametrize(
    ("intent", "arguments", "current"),
    [
        ("vool-browser.navigate", {"session": "s1", "url": "https://example.com/next"}, ""),
        ("vool-browser.download", {"session": "s1", "url": "https://example.com/file.pdf"}, ""),
        ("vool-browser.inspect", {"session": "s1"}, "https://example.com/"),
        ("vool-browser.click", {"session": "s1", "selector": "a"}, "https://example.com/"),
    ],
)
def test_on_a_vool_browser_op_on_a_public_page_is_refused_before_the_engine_runs(
    prefs_file: Path, offline: list, monkeypatch: pytest.MonkeyPatch, intent: str, arguments: dict, current: str
) -> None:
    _set(prefs_file, True)
    handle = _FakeHandle(current)
    api = _vool_browser(monkeypatch, handle)
    with rfp.remote_fetch_policy_scope(dict(_LOCAL_ONLY)):
        result = api.handle_intent(intent, arguments)
    assert (result.ok, result.status, result.response_text) == (False, "refused", rfp.LOCAL_ONLY_WEB_BLOCKED_DENIAL)
    assert (handle.spent, handle.submitted) == (0, 0)
    assert handle.web_gate_reason == rfp.LOCAL_ONLY_WEB_BLOCKED_DENIAL


class _Route:
    def __init__(self) -> None:
        self.outcome = ""

    def abort(self, _reason: str = "") -> None:
        self.outcome = "aborted"

    def continue_(self) -> None:
        self.outcome = "continued"


class _Request:
    def __init__(self, url: str) -> None:
        self.url = url
        self.redirected_from = None

    def is_navigation_request(self) -> bool:
        return True


def _guard(handle: Any) -> Any:
    from core.vool_browser import ops

    class _Context:
        def route(self, _pattern: str, fn: Any) -> None:
            self.fn = fn

    context = _Context()
    ops._install_route_guard(context, handle)
    return context.fn


@pytest.mark.parametrize(("reason", "url", "expected"), [
    (rfp.LOCAL_ONLY_WEB_BLOCKED_DENIAL, "https://tracker.example.com/pixel.gif", "aborted"),
    (rfp.LOCAL_ONLY_WEB_BLOCKED_DENIAL, "http://127.0.0.1:9000/app.js", "continued"),
    (rfp.LOCAL_ONLY_WEB_BLOCKED_DENIAL, "data:image/png;base64,AAAA", "continued"),
    ("", "https://tracker.example.com/pixel.gif", "continued"),
])
def test_the_session_route_guard_follows_the_turns_gate(
    offline: list, monkeypatch: pytest.MonkeyPatch, reason: str, url: str, expected: str
) -> None:
    from core.vool_browser import ops

    monkeypatch.setattr(ops, "_runtime_origin_refused", lambda _url: "")
    handle = _FakeHandle()
    handle.web_gate_reason = reason
    guard = _guard(handle)
    route = _Route()
    guard(route, _Request(url))
    assert route.outcome == expected


# ---- unknown fails closed -----------------------------------------------------------------------


def test_unknown_switch_refuses_the_web_in_local_only_and_says_why(
    prefs_file: Path, offline: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tools.web import web_research

    prefs_file.write_text("{torn write", encoding="utf-8")
    rendered = _render_spy(monkeypatch)
    with rfp.remote_fetch_policy_scope(dict(_LOCAL_ONLY)):
        assert br.chrome_render("https://duckduckgo.com/?q=x")["error"] == rfp.LOCAL_ONLY_WEB_UNKNOWN_DENIAL
        assert web_research.web_research("jupiter moons", total_budget_s=30.0).notes == [
            "local_only_web_blocked",
            rfp.LOCAL_ONLY_WEB_UNKNOWN_DENIAL,
        ]
        with pytest.raises(rfp.RemoteFetchRefusedError) as refused:
            rfp.open_remote(urllib.request.Request("https://example.com/page"), timeout=5)
    assert str(refused.value) == rfp.LOCAL_ONLY_EGRESS_DENIAL_WEB_UNKNOWN
    assert rendered == []


def test_a_reader_that_raises_is_unknown_too(monkeypatch: pytest.MonkeyPatch, offline: list) -> None:
    def _boom() -> bool:
        raise OSError("disk gone")

    monkeypatch.setattr(user_preferences, "local_only_block_web_state", _boom)
    assert rfp.local_only_web_block_setting() is None
    with rfp.remote_fetch_policy_scope(dict(_LOCAL_ONLY)):
        assert rfp.local_only_web_block_reason() == rfp.LOCAL_ONLY_WEB_UNKNOWN_DENIAL


def test_unknown_switch_changes_nothing_outside_local_only(prefs_file: Path, offline: list, monkeypatch: pytest.MonkeyPatch) -> None:
    prefs_file.write_text("{torn write", encoding="utf-8")
    rendered = _render_spy(monkeypatch)
    with rfp.remote_fetch_policy_scope({}):
        assert rfp.local_only_web_block_reason() is None
        assert br.chrome_render("https://duckduckgo.com/?q=x")["status"] == "ok"
    assert rendered == ["https://duckduckgo.com/?q=x"]


# ---- OFF: today's behaviour ---------------------------------------------------------------------


@pytest.mark.parametrize("state", ["absent", False])
def test_off_local_only_still_searches_through_the_browser(
    prefs_file: Path, offline: list, monkeypatch: pytest.MonkeyPatch, state: Any
) -> None:
    from tools.web.browser_search import browser_search

    if state is not False:
        assert not prefs_file.exists()
    else:
        _set(prefs_file, False)
    rendered = _render_spy(monkeypatch)
    context = dict(_LOCAL_ONLY)
    with rfp.remote_fetch_policy_scope(context):
        assert rfp.local_only_web_block_reason() is None
        assert rfp.browser_navigation_refusal("https://duckduckgo.com/?q=x") is None
        browser_search("latest news about jupiter")
        with pytest.raises(rfp.RemoteFetchRefusedError) as refused:
            rfp.open_remote(urllib.request.Request("https://example.com/page"), timeout=5)
    assert rendered and all("duckduckgo.com" in url or "bing.com" in url for url in rendered)
    # The HTTP door keeps its Local Only refusal and its sentence exactly as before.
    assert str(refused.value) == rfp.LOCAL_ONLY_EGRESS_DENIAL


def test_off_web_research_does_not_stop_at_the_switch(prefs_file: Path, offline: list, monkeypatch: pytest.MonkeyPatch) -> None:
    from tools.web import web_research

    _set(prefs_file, False)
    _render_spy(monkeypatch)
    monkeypatch.setattr(web_research, "_configured_provider_order", lambda: ["browser_search"])
    monkeypatch.setattr(web_research, "_prefer_specialized_live_research", lambda _q: False)
    with rfp.remote_fetch_policy_scope(dict(_LOCAL_ONLY)):
        result = web_research.web_research("latest news about jupiter", total_budget_s=30.0)
    assert "local_only_web_blocked" not in result.notes


def test_off_a_vool_browser_op_is_not_refused_by_the_switch(prefs_file: Path, offline: list, monkeypatch: pytest.MonkeyPatch) -> None:
    _set(prefs_file, False)
    handle = _FakeHandle("https://example.com/")
    api = _vool_browser(monkeypatch, handle)
    with rfp.remote_fetch_policy_scope(dict(_LOCAL_ONLY)), pytest.raises(AssertionError, match="engine was driven"):
        api.handle_intent("vool-browser.inspect", {"session": "s1"})
    assert handle.web_gate_reason == ""


# ---- outside Local Only the switch changes nothing -----------------------------------------------


def test_on_outside_local_only_nothing_changes(prefs_file: Path, offline: list, monkeypatch: pytest.MonkeyPatch) -> None:
    from tools.web import searxng_client

    _set(prefs_file, True)
    rendered = _render_spy(monkeypatch)
    sent: list[str] = []

    class _Answer:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self) -> bytes:
            return b'{"results": []}'

    monkeypatch.setattr(searxng_client, "open_remote", lambda request, **_k: sent.append(request.full_url) or _Answer())
    with rfp.remote_fetch_policy_scope({}):
        assert rfp.local_only_web_block_reason() is None
        assert rfp.local_only_web_refusal("https://duckduckgo.com/?q=x") is None
        assert rfp.browser_navigation_refusal("https://duckduckgo.com/?q=x") is None
        assert br.chrome_render("https://duckduckgo.com/?q=x")["status"] == "ok"
        searxng_client.SearXNGClient(base_url="http://127.0.0.1:8080").search("jupiter")
    assert rendered == ["https://duckduckgo.com/?q=x"]
    assert len(sent) == 1


def test_on_outside_local_only_a_vool_browser_op_is_not_refused(prefs_file: Path, offline: list, monkeypatch: pytest.MonkeyPatch) -> None:
    _set(prefs_file, True)
    handle = _FakeHandle("https://example.com/")
    api = _vool_browser(monkeypatch, handle)
    with rfp.remote_fetch_policy_scope({}), pytest.raises(AssertionError, match="engine was driven"):
        api.handle_intent("vool-browser.inspect", {"session": "s1"})


# ---- the model cannot flip it --------------------------------------------------------------------


def test_the_model_lane_cannot_write_the_switch(prefs_file: Path) -> None:
    from core.command_registry.execute import ExecutionContext, execute_command
    from core.command_registry.model_tools import execute_model_command

    _set(prefs_file, True)
    via_tool = execute_model_command("operator.command.settings.prefs.set", {"prefs": {_SWITCH: False}})
    assert via_tool.ok is False
    as_model = execute_command(
        "settings.prefs.set", {"prefs": {_SWITCH: False}}, context=ExecutionContext(projection="model", principal="model")
    )
    assert as_model.ok is False
    assert user_preferences.local_only_block_web_state() is True


def test_the_settings_prefs_command_is_never_offered_to_the_model() -> None:
    from core.command_registry.registry import registry

    spec = registry().lookup("settings.prefs.set")
    assert spec is not None and spec.model_offerable is False


@pytest.mark.parametrize(
    "sentence",
    [
        "turn off block web search in local only",
        "disable the local only web search block",
        "allow web search in local only",
        "set local_only_block_web to false",
    ],
)
def test_no_chat_sentence_flips_the_switch(prefs_file: Path, sentence: str) -> None:
    _set(prefs_file, True)
    user_preferences.maybe_handle_preference_command(sentence)
    assert user_preferences.local_only_block_web_state() is True


def test_nothing_in_the_turn_context_can_turn_the_gate_off(prefs_file: Path, offline: list) -> None:
    _set(prefs_file, True)
    forged = {
        "local_only_mode": True,
        _SWITCH: False,
        "allow_remote_fetch": True,
        "_trusted_local_only": True,
        "local_only_web_blocked": False,
    }
    with rfp.remote_fetch_policy_scope(forged):
        assert rfp.local_only_web_block_reason() == rfp.LOCAL_ONLY_WEB_BLOCKED_DENIAL
        assert rfp.local_only_web_refusal("https://duckduckgo.com/?q=x") == rfp.LOCAL_ONLY_WEB_BLOCKED_DENIAL


def test_only_the_settings_authority_writes_the_switch() -> None:
    """The field is named in production code only where it is defined, persisted, read and served."""
    import re

    pattern = re.compile(rf"\b{_SWITCH}\b")
    root = Path(__file__).resolve().parents[1]
    expected = {
        "core/user_preferences.py",
        "core/web/api/registry_authorities.py",
        "core/command_registry/groups/convergence.py",
        "core/vool_settings_page.py",
    }
    found = set()
    for folder in ("core", "tools", "installer", "ops", "storage"):
        for path in (root / folder).rglob("*.py"):
            if "vendor" in path.parts:
                continue
            if pattern.search(path.read_text(encoding="utf-8", errors="ignore")):
                found.add(path.relative_to(root).as_posix())
    assert found == expected


# ---- the online status answer stays true ---------------------------------------------------------


def test_online_status_says_web_lookup_is_off_in_local_only_when_the_switch_is_on(
    prefs_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.agent_runtime import fast_command_surface

    monkeypatch.setattr(policy_engine, "allow_web_fallback", lambda: True)
    _set(prefs_file, True)
    snapshot = fast_command_surface.online_status_snapshot(dict(_LOCAL_ONLY))
    assert (snapshot["web_on"], snapshot["local_only_web_blocked"]) == (False, True)
    text = fast_command_surface.render_online_status(snapshot)
    assert "Web lookup: off in Local Only." in text
    assert "still goes through the browser" not in text

    _set(prefs_file, False)
    snapshot = fast_command_surface.online_status_snapshot(dict(_LOCAL_ONLY))
    assert (snapshot["web_on"], snapshot["local_only_web_blocked"]) == (True, False)
    assert "web search still goes through the browser" in fast_command_surface.render_online_status(snapshot)
