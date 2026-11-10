"""Windows 0.7.1-beta capability test: no cloud provider contact before the owner opts in.

Seen on a fresh Windows 11 VM (no key stored, cloud never turned on, the default routing mode
"Local first · free cloud only if needed"): the chat turn "use a cloud model to say hi" resolved
openrouter.ai, opened TLS to it and downloaded the model catalog (about 883 KB), and VOOL wrote
``%LOCALAPPDATA%\\VOOL\\data\\openrouter_models_cache.json``. The ship bar says no network before
opt-in.

What this file pins, through the production doors (``dispatch_post`` / ``dispatch_get`` with a real
``VoolAgent``) and the catalog functions every caller shares:

- With the Windows seam on (``core.ollama_cloud_models.CLOUD_REFUSAL_ACTIVE``) and a fresh profile
  (no key, no opt-in), a chat turn, opening Settings and opening Models & Providers make no
  outbound connection and write no ``openrouter_models_cache.json``; the chat turn answers
  "Cloud models are off. Add your own key in Settings to use one."
- The chat page served on Windows with cloud off defaults to the local-only lane and shows nothing
  labelled "free cloud".
- With a key stored and cloud turned on, the catalog fetch is reached (a mock, no network).
- Off Windows (seam off) nothing changes: the keyless catalog fetch is still attempted.

Sockets are patched to raise and record (``socket.socket.connect`` and ``socket.getaddrinfo``).
The root conftest also swaps ``urllib.request.urlopen`` for a guard that refuses public hosts
WITHOUT opening a socket, which hid this exact fetch from socket-level checks; the real
``urlopen`` is put back here so a request that leaves the door reaches the patched socket layer.
"""
from __future__ import annotations

import datetime
import json
import socket
import urllib.request

import pytest

from core import ollama_cloud_models
from tests.operator_profile_rig import chat, make_agent, profile_env_generator, served_get

OFF_TEXT = "Cloud models are off. Add your own key in Settings to use one."
CLOUD_PROMPT = "use a cloud model to say hi"
FREE_ID = "test/free-one:free"
PAID_ID = "test/paid-one"


def _real_urlopen(request, *args, timeout=None, context=None, **_kwargs):
    """urllib's own opener, built at call time: a module-level ``urlopen`` captured at collection
    may already be another test module's stub, and that stub then answers instead of the socket."""
    handlers = [urllib.request.HTTPSHandler(context=context)] if context is not None else []
    opener = urllib.request.build_opener(*handlers)
    return opener.open(request, *args, timeout=timeout if timeout is not None else 30.0)

_LOOPBACK = {"127.0.0.1", "localhost", "::1", ""}
_PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")


def _catalog_payload() -> dict:
    return {"data": [
        {"id": FREE_ID, "name": "Test Free One", "context_length": 8192, "pricing": {"prompt": "0", "completion": "0"},
         "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]}},
        {"id": PAID_ID, "name": "Test Paid One", "context_length": 32768,
         "pricing": {"prompt": "0.000001", "completion": "0.000002"},
         "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]}},
    ]}


class _FakeResponse:
    status = 200

    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self, *_args) -> bytes:
        return self._body


@pytest.fixture()
def profile_env(tmp_path, monkeypatch):
    yield from profile_env_generator(tmp_path, monkeypatch)


@pytest.fixture()
def sockets(monkeypatch):
    """socket.getaddrinfo and socket.socket.connect both raise and record."""
    records: list[tuple[str, str]] = []

    def _getaddrinfo(host, *_args, **_kwargs):
        records.append(("getaddrinfo", str(host or "")))
        raise OSError(f"test: getaddrinfo({host!r}) refused")

    def _connect(self, address):
        host = address[0] if isinstance(address, tuple) and address else address
        records.append(("connect", str(host or "")))
        raise OSError(f"test: connect({address!r}) refused")

    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", _connect)
    monkeypatch.setattr(urllib.request, "urlopen", _real_urlopen)
    monkeypatch.setattr(urllib.request, "_opener", None)
    for name in _PROXY_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("VOOL_MODEL_RADAR_POLLER", "0")
    return records


def _outbound(records: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [row for row in records if row[1].strip("[]").lower() not in _LOOPBACK]


@pytest.fixture()
def windows(monkeypatch):
    monkeypatch.setattr(ollama_cloud_models, "CLOUD_REFUSAL_ACTIVE", True)


@pytest.fixture()
def off_windows(monkeypatch):
    monkeypatch.setattr(ollama_cloud_models, "CLOUD_REFUSAL_ACTIVE", False)


def _cache_files(env) -> list:
    return sorted(env["home"].rglob("openrouter_models_cache.json"))


def _store_openrouter_key() -> None:
    from core import credential_store

    credential_store.store_credential("llm.cloud.openrouter", "sk-or-v1-" + "0" * 64)


def _turn_cloud_on() -> None:
    from core import cloud_escalation_policy as cep

    cep.set_free_cloud_enabled(True)
    assert cep.load_policy().free_cloud_enabled is True


def _seed_cache(env) -> None:
    from core.runtime_paths import active_data_dir

    path = active_data_dir() / "openrouter_models_cache.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "payload": _catalog_payload()}))


SETTINGS_READS = (
    ("/settings", {}),
    ("/api/cloud/providers", {}),
    ("/api/settings/credentials", {}),
    ("/api/search/providers", {}),
    ("/api/cloud/status", {}),
)
MODELS_AND_PROVIDERS_READS = (
    ("/api/cloud/model", {}),
    ("/api/cloud/usepod/discovery", {}),
    ("/api/cloud/spend-limits", {}),
    ("/api/models/local", {}),
    ("/api/cloud/models", {}),
    ("/api/cloud/models", {"provider": ["openrouter"]}),
    ("/api/cloud/models", {"provider": ["openai"]}),
    ("/api/cloud/models", {"provider": ["usepod"]}),
)


# ---- Windows, fresh profile: no key, no opt-in ----------------------------------------------------

def test_chat_turn_asking_for_cloud_makes_no_contact_and_says_cloud_is_off(profile_env, windows, sockets):
    reply = chat(make_agent(), CLOUD_PROMPT, "cloud-off-chat")
    assert _outbound(sockets) == [], _outbound(sockets)
    assert _cache_files(profile_env) == []
    assert OFF_TEXT in reply["text"], reply["text"]


def test_opening_settings_makes_no_contact(profile_env, windows, sockets):
    agent = make_agent()
    for path, query in SETTINGS_READS:
        response = served_get(agent, path, query)
        assert int(response.status) == 200, (path, response.status)
    assert _outbound(sockets) == [], _outbound(sockets)
    assert _cache_files(profile_env) == []


def test_opening_models_and_providers_makes_no_contact(profile_env, windows, sockets):
    agent = make_agent()
    for path, query in MODELS_AND_PROVIDERS_READS:
        response = served_get(agent, path, query)
        assert int(response.status) == 200, (path, query, response.status)
    assert _outbound(sockets) == [], _outbound(sockets)
    assert _cache_files(profile_env) == []


def test_models_and_providers_refresh_press_makes_no_contact(profile_env, windows, sockets):
    agent = make_agent()
    for provider in ("openrouter", "usepod"):
        response = served_get(agent, "/api/cloud/models", {"provider": [provider], "refresh": ["1"]})
        assert int(response.status) == 200, (provider, response.status)
    assert _outbound(sockets) == [], _outbound(sockets)
    assert _cache_files(profile_env) == []


def test_openrouter_catalog_list_hides_free_cloud_while_off(profile_env, windows, sockets):
    """A catalog already on disk (an earlier install) is not shown or used while cloud is off."""
    _seed_cache(profile_env)
    response = served_get(make_agent(), "/api/cloud/models", {"provider": ["openrouter"]})
    payload = json.loads(response.body.decode("utf-8"))
    assert payload["models"] == [] and payload["free_count"] == 0, payload
    assert payload.get("free_cloud_enabled") is False
    assert _outbound(sockets) == []


def test_catalog_entry_points_refuse_locally_before_opt_in(profile_env, windows, sockets):
    from core import openrouter_catalog as oc
    from core import windows_cloud_gate
    from core.cloud_model_control import force_catalog_refresh
    from core.model_radar_service import _poll_openrouter_once

    with pytest.raises(windows_cloud_gate.CloudContactRefused) as refused:
        oc.refresh_openrouter_catalog()
    assert str(refused.value) == OFF_TEXT
    with pytest.raises(windows_cloud_gate.CloudContactRefused):
        oc.load_openrouter_catalog()
    assert oc.safe_all_models(allow_network=True) == ((), None)
    assert oc.safe_free_models(allow_network=True) == ((), None)
    assert oc.pick_auto_free_models(allow_network=True) == {}
    assert force_catalog_refresh()["ok"] is False
    assert _poll_openrouter_once() is False
    assert _outbound(sockets) == [], _outbound(sockets)
    assert _cache_files(profile_env) == []


def test_cached_catalog_is_not_used_while_off(profile_env, windows, sockets):
    from core import openrouter_catalog as oc

    _seed_cache(profile_env)
    oc._CACHED_ROW_MEMO.clear()
    try:
        assert oc.safe_all_models(allow_network=False) == ((), None)
        assert oc.pick_auto_free_models(allow_network=False) == {}
        assert oc.cached_catalog_row(FREE_ID) is None
    finally:
        oc._CACHED_ROW_MEMO.clear()


def test_outbound_door_refuses_every_cloud_provider_before_opt_in(profile_env, windows, sockets):
    from core import windows_cloud_gate
    from core.effect_gateway import named_background_effect_scope
    from core.remote_fetch_policy import RemoteFetchRefusedError, open_remote_url

    urls = (
        "https://openrouter.ai/api/v1/models",
        "https://api.openai.com/v1/models",
        "https://api.anthropic.com/v1/models",
        "https://api.groq.com/openai/v1/models",
        "https://generativelanguage.googleapis.com/v1beta/openai/models",
        "https://api.deepseek.com/v1/models",
        "https://api.moonshot.ai/v1/models",
        "https://api.usepod.ai/v1/marketplace",
    )
    with named_background_effect_scope("model_catalog.refresh"):
        for url in urls:
            with pytest.raises(windows_cloud_gate.CloudContactRefused) as refused:
                open_remote_url(url, timeout=1.0)
            assert isinstance(refused.value, RemoteFetchRefusedError)
    assert _outbound(sockets) == [], _outbound(sockets)


def test_key_stored_but_cloud_not_turned_on_still_refuses(profile_env, windows, sockets):
    from core import cloud_escalation_policy as cep
    from core import openrouter_catalog as oc
    from core import windows_cloud_gate

    _store_openrouter_key()
    cep.save_policy(cep.CloudEscalationPolicy(mode=cep.MODE_OFF, free_cloud_enabled=False))
    assert windows_cloud_gate.cloud_key_stored() is True
    with pytest.raises(windows_cloud_gate.CloudContactRefused):
        oc.refresh_openrouter_catalog()
    assert _outbound(sockets) == []


def test_cloud_turned_on_without_a_key_still_refuses(profile_env, windows, sockets):
    from core import openrouter_catalog as oc
    from core import windows_cloud_gate

    _turn_cloud_on()
    assert windows_cloud_gate.cloud_key_stored() is False
    with pytest.raises(windows_cloud_gate.CloudContactRefused):
        oc.refresh_openrouter_catalog()
    reply = chat(make_agent(), CLOUD_PROMPT, "cloud-on-no-key")
    assert OFF_TEXT in reply["text"], reply["text"]
    assert _outbound(sockets) == [], _outbound(sockets)
    assert _cache_files(profile_env) == []


def test_chat_page_shows_no_free_cloud_and_keeps_the_auto_lane_on_windows(profile_env, windows, sockets):
    """The lane every chat without a stored choice runs on is Auto ("Local first",
    ``modelForChat``'s ``'vool'``); with cloud off it is labelled "cloud models off" and runs no cloud
    model. "This computer only" is not the default: it also cuts web access for the turn."""
    response = served_get(make_agent(), "/chat", {})
    html = response.body.decode("utf-8")
    assert "free cloud only if needed" not in html
    assert "verified-free cloud fallback" not in html
    assert "return choices[String(chatId)] || 'vool';" in html
    assert ('aria-checked="true" data-run-model="vool"><b data-i18n="header.run_local_first">Local first</b>'
            '<small>cloud models off</small>') in html
    assert 'aria-checked="false" data-run-model="vool-local-only"' in html
    assert _outbound(sockets) == []


def test_the_default_lane_keeps_web_access_when_the_owner_turns_it_on(profile_env, windows, sockets):
    from core import auto_local_only_mode, policy_engine, windows_cloud_gate
    from core.remote_fetch_policy import _local_only_egress_forbidden, effective_web_available

    policy_engine.set_operator_policy_values({"system.allow_web_fallback": True})
    policy_engine.load(force_reload=True)
    assert windows_cloud_gate.cloud_off() is True
    default_turn: dict = {}
    assert auto_local_only_mode.bind_turn(default_turn, "vool") is False
    assert effective_web_available(default_turn) is True
    assert _local_only_egress_forbidden(default_turn, "https://93.184.216.34/search?q=x") == (False, "")
    # The default lane still contacts no cloud provider.
    assert windows_cloud_gate.door_refusal("https://openrouter.ai/api/v1/models") is not None
    # "This computer only" is zero public egress, web included (its own contract, unchanged).
    computer_only: dict = {}
    assert auto_local_only_mode.bind_turn(computer_only, "vool-local-only") is True
    assert effective_web_available(computer_only) is False
    assert _local_only_egress_forbidden(computer_only, "https://93.184.216.34/search?q=x")[0] is True
    assert _outbound(sockets) == []


def test_cloud_off_command_closes_the_gate_on_windows(profile_env, windows, sockets):
    from core import cloud_escalation_policy as cep
    from core import windows_cloud_gate

    _store_openrouter_key()
    _turn_cloud_on()
    assert windows_cloud_gate.cloud_off() is False
    agent = make_agent()
    reply = chat(agent, "cloud off", "cloud-off-command")
    assert windows_cloud_gate.cloud_off() is True
    assert cep.load_policy().free_cloud_enabled is False
    assert cep.load_policy().mode == cep.MODE_OFF
    assert OFF_TEXT in reply["text"], reply["text"]
    turn = chat(agent, CLOUD_PROMPT, "cloud-off-command-after")
    assert OFF_TEXT in turn["text"], turn["text"]
    assert _outbound(sockets) == [], _outbound(sockets)
    assert _cache_files(profile_env) == []


def test_removing_the_key_in_settings_closes_the_gate(profile_env, windows, sockets):
    from core import windows_cloud_gate
    from core.runtime_paths import data_path
    from tests.operator_profile_rig import served_post

    _store_openrouter_key()
    _turn_cloud_on()
    assert windows_cloud_gate.cloud_off() is False
    agent = make_agent()
    response = served_post(agent, "/api/settings/credentials", {"name": "llm.cloud.openrouter", "delete": True})
    assert int(response.status) == 200, response.body
    for filename in ("credentials.meta.json", "credentials.enc.json"):
        path = data_path(filename)
        names = set(json.loads(path.read_text(encoding="utf-8")) or {}) if path.exists() else set()
        assert not {name for name in names if name.startswith("llm.cloud.")} - {
            "llm.cloud.usepod_origin", "llm.cloud.custom_base_url"}, (filename, names)
    assert windows_cloud_gate.cloud_key_stored() is False
    assert windows_cloud_gate.cloud_off() is True
    turn = chat(agent, CLOUD_PROMPT, "key-removed")
    assert OFF_TEXT in turn["text"], turn["text"]
    assert _outbound(sockets) == [], _outbound(sockets)
    assert _cache_files(profile_env) == []


def test_a_keyless_request_inside_the_verify_scope_is_refused(profile_env, windows, sockets):
    from core import windows_cloud_gate
    from core.effect_gateway import named_background_effect_scope
    from core.remote_fetch_policy import open_remote_url

    with windows_cloud_gate.owner_key_check("openrouter.ai"), named_background_effect_scope("credential.verify"):
        with pytest.raises(windows_cloud_gate.CloudContactRefused):
            open_remote_url("https://openrouter.ai/api/v1/key", timeout=1.0, keyed_or_keyless="keyless")
        with pytest.raises(windows_cloud_gate.CloudContactRefused):
            open_remote_url("https://openrouter.ai/api/v1/key", timeout=1.0)
        with pytest.raises(windows_cloud_gate.CloudContactRefused):
            open_remote_url("https://api.openai.com/v1/models", timeout=1.0, keyed_or_keyless="keyed")
    assert _outbound(sockets) == [], _outbound(sockets)


def test_a_verify_started_outside_the_settings_route_is_refused(profile_env, windows, sockets):
    """A model tool, a discovery scan or a connection probe calling the verifier itself."""
    from core.credential_intelligence.provider_registry import default_registry
    from core.credential_intelligence.verification import verify_provider_credential

    descriptor = default_registry().get("openrouter")
    assert descriptor is not None
    for scope in ("credential.verify", "cloud.connection_probe", "discovery.refresh"):
        outcome = verify_provider_credential("sk-or-v1-" + "0" * 64, descriptor, scope=scope)
        assert str(outcome.status) == "refused", (scope, outcome)
    assert _outbound(sockets) == [], _outbound(sockets)


def _intake_verify(agent, *, client_host: str = "127.0.0.1"):
    from tests.operator_profile_rig import body_json, served_post

    begun = body_json(served_post(agent, "/api/intake/begin", {}))
    sid = begun["session_id"]
    served_post(agent, "/api/intake/classify", {"session_id": sid, "value": "sk-or-v1-" + "0" * 64})
    response = served_post(agent, "/api/intake/verify", {"session_id": sid, "provider_id": "openrouter"},
                           client_host=client_host)
    return response, body_json(response)


def test_the_settings_verify_reaches_only_that_providers_verify_endpoint(profile_env, windows, sockets):
    response, payload = _intake_verify(make_agent())
    assert int(response.status) == 200, payload
    # The patched socket answers nothing, so the check ends as a network failure, after one
    # lookup of the provider's own verification host and nothing else.
    outbound = _outbound(sockets)
    assert outbound and {host for _kind, host in outbound} == {"openrouter.ai"}, outbound
    assert payload.get("outcome") != "refused", payload
    assert _cache_files(profile_env) == []


def test_the_settings_verify_from_another_machine_opens_nothing(profile_env, windows, sockets):
    response, _payload = _intake_verify(make_agent(), client_host="192.168.1.20")
    assert int(response.status) == 403
    assert _outbound(sockets) == [], _outbound(sockets)


# ---- Windows, key stored and cloud turned on: the fetch path is reached (mocked) ----------------

def test_key_and_cloud_on_reaches_the_catalog_fetch(profile_env, windows, sockets, monkeypatch):
    from core import openrouter_catalog as oc

    _store_openrouter_key()
    _turn_cloud_on()
    fetched: list[str] = []

    def _fake_open_remote_url(url, **_kwargs):
        fetched.append(url)
        return _FakeResponse(_catalog_payload())

    monkeypatch.setattr(oc, "open_remote_url", _fake_open_remote_url)
    response = served_get(make_agent(), "/api/cloud/models", {"provider": ["openrouter"], "refresh": ["1"]})
    payload = json.loads(response.body.decode("utf-8"))
    assert fetched == ["https://openrouter.ai/api/v1/models"]
    assert len(_cache_files(profile_env)) == 1
    assert [row["id"] for row in payload["models"]] == [FREE_ID, PAID_ID]
    assert _outbound(sockets) == []


def test_key_and_cloud_on_lets_the_door_open(profile_env, windows, sockets):
    from core.effect_gateway import named_background_effect_scope
    from core.remote_fetch_policy import open_remote_url

    _store_openrouter_key()
    _turn_cloud_on()
    with named_background_effect_scope("model_catalog.refresh"), pytest.raises(Exception):
        open_remote_url("https://openrouter.ai/api/v1/models", timeout=1.0)
    assert ("getaddrinfo", "openrouter.ai") in sockets, sockets


def test_owner_key_check_is_scoped_to_one_host_and_keyed_requests(profile_env, windows):
    from core import windows_cloud_gate

    key_url = "https://openrouter.ai/api/v1/key"
    assert windows_cloud_gate.door_refusal(key_url, "openrouter", "keyed") is not None
    with windows_cloud_gate.owner_key_check("openrouter.ai"):
        assert windows_cloud_gate.door_refusal(key_url, "openrouter", "keyed") is None
        assert windows_cloud_gate.door_refusal(key_url, "openrouter", "keyless") is not None
        assert windows_cloud_gate.door_refusal("https://api.openai.com/v1/models", "openai", "keyed") is not None
    assert windows_cloud_gate.door_refusal(key_url, "openrouter", "keyed") is not None


# ---- Off Windows (seam off): the old behaviour stands ---------------------------------------------

def test_off_windows_the_chat_turn_still_reaches_the_catalog(profile_env, off_windows, sockets):
    reply = chat(make_agent(), CLOUD_PROMPT, "off-windows-chat")
    assert ("getaddrinfo", "openrouter.ai") in sockets, sockets
    assert OFF_TEXT not in reply["text"]


def test_off_windows_keyless_catalog_fetch_is_reached(profile_env, off_windows, sockets, monkeypatch):
    from core import openrouter_catalog as oc

    fetched: list[str] = []

    def _fake_open_remote_url(url, **_kwargs):
        fetched.append(url)
        return _FakeResponse(_catalog_payload())

    monkeypatch.setattr(oc, "open_remote_url", _fake_open_remote_url)
    models = oc.refresh_openrouter_catalog()
    assert fetched == ["https://openrouter.ai/api/v1/models"]
    assert [m.model_id for m in models] == [FREE_ID, PAID_ID]
    assert len(_cache_files(profile_env)) == 1
    assert oc.pick_auto_free_models(allow_network=False) == {"general": FREE_ID}


def test_off_windows_the_door_is_unchanged(profile_env, off_windows, sockets):
    from core.effect_gateway import named_background_effect_scope
    from core.remote_fetch_policy import open_remote_url

    with named_background_effect_scope("model_catalog.refresh"), pytest.raises(Exception) as raised:
        open_remote_url("https://api.openai.com/v1/models", timeout=1.0)
    assert OFF_TEXT not in str(raised.value)
    assert ("getaddrinfo", "api.openai.com") in sockets, sockets


def test_off_windows_chat_page_keeps_local_first_default(profile_env, off_windows, sockets):
    html = served_get(make_agent(), "/chat", {}).body.decode("utf-8")
    assert "free cloud only if needed" in html
    assert "let modelValue = localStorage.getItem('vool_model') || 'vool';" in html
    assert 'aria-checked="true" data-run-model="vool"' in html


def test_off_windows_cloud_off_command_is_unchanged(profile_env, off_windows, sockets):
    from core import cloud_escalation_policy as cep

    _store_openrouter_key()
    _turn_cloud_on()
    reply = chat(make_agent(), "cloud off", "off-windows-cloud-off")
    assert cep.load_policy().mode == cep.MODE_OFF
    assert cep.load_policy().free_cloud_enabled is True
    assert OFF_TEXT not in reply["text"]


def test_a_provider_endpoint_on_this_machine_or_the_lan_is_not_cloud(profile_env, windows, sockets, monkeypatch):
    """A UsePod origin or custom endpoint the owner points at their own hardware is local: the
    Windows rule does not refuse it (IM Windows run 42783582031: a loopback UsePod stub's price feed
    was cut). A public provider host is still refused, and deciding that resolves no name."""
    from core import cloud_providers, windows_cloud_gate
    from core.usepod import discovery

    monkeypatch.setattr(discovery, "configured_origin", lambda: "http://127.0.0.1:8123")
    monkeypatch.setattr(cloud_providers, "custom_base_url", lambda: "http://192.168.1.5:1234/v1")
    assert windows_cloud_gate.cloud_off() is True
    for url in ("http://127.0.0.1:8123/v1/marketplace", "http://localhost:8123/v1/models",
                "http://192.168.1.5:1234/v1/models", "http://[::1]:8123/v1/models"):
        assert windows_cloud_gate.is_cloud_provider_request(url) is False, url
        assert windows_cloud_gate.door_refusal(url, "usepod", "keyless") is None, url
    for url in ("https://api.usepod.ai/v1/marketplace", "https://openrouter.ai/api/v1/models"):
        assert windows_cloud_gate.door_refusal(url, "", "keyless") is not None, url
    assert sockets == [], sockets


REASONING_ID = "test/catalog-declared-one"
LOCAL_LANE = "http://127.0.0.1:9/api/v1"
CLOUD_LANE = "https://openrouter.ai/api/v1"


def _seed_reasoning_cache() -> None:
    from core.runtime_paths import active_data_dir

    payload = {"data": [{
        "id": REASONING_ID, "name": "Test Catalog Declared One", "context_length": 32768,
        "pricing": {"prompt": "0.000001", "completion": "0.000002"},
        "supported_parameters": ["reasoning", "include_reasoning", "max_tokens"],
        "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]}}]}
    path = active_data_dir() / "openrouter_models_cache.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "payload": payload}))


def _byok_manifest(base_url: str):
    from storage.model_provider_manifest import ModelProviderManifest

    return ModelProviderManifest(
        provider_name="openrouter-byok", model_name=REASONING_ID, source_type="http",
        adapter_type="openai_compatible", license_name="Provider", license_reference="user-managed",
        weight_location="external", runtime_dependency="remote-openai-compatible-provider",
        capabilities=["summarize"], runtime_config={"base_url": base_url, "timeout_seconds": 5},
        metadata={"cost_class": "paid_cloud"},
    )


def test_a_lane_on_this_machine_reads_its_cached_catalog_row_while_off(profile_env, windows, sockets):
    """Cloud off on Windows: an OpenRouter-dialect lane whose endpoint is on this machine is not
    cloud (the dispatch already lets it through), so its cached catalog facts -- a disk read, no
    network -- still size its request. A cloud-host lane, or no lane at all, still reads nothing."""
    from adapters.openai_compatible_adapter import OpenAICompatibleAdapter
    from core import openrouter_catalog as oc
    from core import windows_cloud_gate
    from core.output_budget_policy import manifest_declares_reasoning

    assert windows_cloud_gate.cloud_off() is True
    _seed_reasoning_cache()
    oc._CACHED_ROW_MEMO.clear()
    try:
        row = oc.cached_catalog_row(REASONING_ID, lane_base_url=LOCAL_LANE)
        assert row is not None and "reasoning" in row.supported_parameters
        assert oc.cached_catalog_row(REASONING_ID, lane_base_url=CLOUD_LANE) is None
        assert oc.cached_catalog_row(REASONING_ID) is None

        assert manifest_declares_reasoning(_byok_manifest(LOCAL_LANE)) is True
        assert OpenAICompatibleAdapter(_byok_manifest(LOCAL_LANE))._declares_reasoning_support() is True
        assert manifest_declares_reasoning(_byok_manifest(CLOUD_LANE)) is False
        assert OpenAICompatibleAdapter(_byok_manifest(CLOUD_LANE))._declares_reasoning_support() is False
    finally:
        oc._CACHED_ROW_MEMO.clear()
    assert _outbound(sockets) == []


def test_local_only_keeps_its_own_refusal_for_a_cloud_url_while_off(profile_env, windows, sockets):
    """Cloud off and Local Only both refuse a cloud URL before any socket; under Local Only the owner
    reads the Local Only sentence (the door checks it first), not the cloud-off line."""
    from core import windows_cloud_gate
    from core.auto_local_only_mode import CONTEXT_KEY, SELECTED_KEY, SELECTOR_VALUE
    from core.remote_fetch_policy import RemoteFetchRefusedError, open_remote, remote_fetch_policy_scope

    url = "https://openrouter.ai/api/v1/chat/completions"
    local_only = {"session_id": "cloud-off-local-only", CONTEXT_KEY: True, SELECTED_KEY: True,
                  "requested_model": SELECTOR_VALUE}
    with remote_fetch_policy_scope(local_only):
        with pytest.raises(RemoteFetchRefusedError) as caught:
            open_remote(urllib.request.Request(url), timeout=1.0)
    assert not isinstance(caught.value, windows_cloud_gate.CloudContactRefused)
    assert "local only" in str(caught.value).lower(), str(caught.value)
    with remote_fetch_policy_scope({"session_id": "cloud-off-default-lane"}):
        with pytest.raises(windows_cloud_gate.CloudContactRefused):
            open_remote(urllib.request.Request(url), timeout=1.0)
    assert _outbound(sockets) == [], _outbound(sockets)


def test_a_confirmed_paid_pin_keeps_cloud_on_and_cloud_off_clears_it(profile_env, windows, sockets):
    """Settings' paid handshake: confirming a paid pin turns the free lane off on purpose. The pin is
    the owner turning cloud on for that model, so the gate stays open on it; ``cloud off`` closes it."""
    from core import cloud_escalation_policy as cep
    from core import windows_cloud_gate
    from core.cloud_model_control import set_cloud_model

    _store_openrouter_key()
    _turn_cloud_on()
    _seed_cache(profile_env)
    ok, message, chosen = set_cloud_model(PAID_ID, owner_local=True, confirm_paid=True)
    assert ok and chosen == PAID_ID, message
    assert cep.load_policy().free_cloud_enabled is False
    assert cep.load_policy().model == PAID_ID
    assert windows_cloud_gate.cloud_off() is False
    reply = chat(make_agent(), "cloud off", "cloud-off-after-paid-pin")
    assert OFF_TEXT in reply["text"], reply["text"]
    assert cep.load_policy().model == ""
    assert windows_cloud_gate.cloud_off() is True


def test_a_pin_without_a_stored_key_still_refuses(profile_env, windows, sockets):
    from core import cloud_escalation_policy as cep
    from core import openrouter_catalog as oc
    from core import windows_cloud_gate

    cep.save_policy(cep.CloudEscalationPolicy(mode=cep.MODE_OFF, free_cloud_enabled=False, model=PAID_ID))
    assert windows_cloud_gate.cloud_key_stored() is False
    with pytest.raises(windows_cloud_gate.CloudContactRefused):
        oc.refresh_openrouter_catalog()
    assert _outbound(sockets) == []


def test_chat_page_serves_its_bootstrap_verbatim_and_states_auto_is_free_only_while_off(profile_env, windows, sockets):
    """The cloud-off texts are applied in the bundle's owner, so the served page embeds exactly what
    ``i18n_bootstrap_js`` builds; and Auto's free-only rule is still stated."""
    from core.i18n.page_bundle import i18n_bootstrap_js

    html = served_get(make_agent(), "/chat", {}).body.decode("utf-8")
    bootstrap = i18n_bootstrap_js("en")
    assert bootstrap in html
    assert '"header.run_local_first_hint":"cloud models off"' in bootstrap
    assert "free cloud only if needed" not in bootstrap
    assert "Auto never selects a paid model" in html
    assert _outbound(sockets) == []
