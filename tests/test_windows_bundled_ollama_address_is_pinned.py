"""The bundle supervisor pins the bundled Ollama's address for serve and for VOOL's client.

Capability test (0.7.1, Ollama 0.35.1): ``OLLAMA_NO_CLOUD=1`` does not stop a cloud pull, so who can
reach serve matters. The supervisor copied the inherited environment into serve's: a machine-wide
``OLLAMA_HOST=0.0.0.0:11434`` would have put serve on every interface, and an inherited
``OLLAMA_ORIGINS=*`` would have let any web page call it. VOOL's own client resolved its address
from the same inherited ``OLLAMA_HOST``.

Now serve gets ``OLLAMA_HOST=127.0.0.1:11434`` and no ``OLLAMA_ORIGINS`` (Ollama's default allows
local origins only), with ``OLLAMA_NO_CLOUD=1`` as before. Every child gets the same address as
``VOOL_BUNDLE_OLLAMA_URL``, which VOOL's client takes over any other variable. Without the
supervisor (a user's own Ollama, any platform) the client resolves as before.
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from installer.bundle import bundle_supervisor
from installer.bundle.bundle_supervisor import BundleSupervisor

INHERITED = {
    "OLLAMA_HOST": "0.0.0.0:11434",
    "OLLAMA_ORIGINS": "*",
    "ollama_origins": "*",  # Windows names are case-insensitive
    "VOOL_OLLAMA_URL": "http://192.168.1.20:11434",
    "VOOL_OLLAMA_CHAT_URL": "http://192.168.1.20:11434/api/chat",
    "VOOL_OLLAMA_PS_URL": "http://192.168.1.20:11434/api/ps",
    "VOOL_RAW_OLLAMA_API_URL": "http://192.168.1.20:11434",
    "OLLAMA_NO_CLOUD": "0",
}


def _child(pid: int) -> mock.Mock:
    child = mock.Mock(pid=pid)
    child.poll.return_value = None
    child.wait.return_value = 0
    return child


def _supervisor(tmp_path: Path, monkeypatch, extra_env: dict[str, str] | None = None) -> BundleSupervisor:
    root = tmp_path / "bundle"
    root.mkdir()
    home = tmp_path / "home"
    env = {"VOOL_HOME": str(home), "LOCALAPPDATA": str(home), **(extra_env or {})}
    supervisor = BundleSupervisor(root, env=env)
    supervisor._prepare()
    supervisor.log_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(bundle_supervisor, "_is_windows_platform", lambda: False)
    monkeypatch.setattr(bundle_supervisor, "_JOB_HOST", False)
    return supervisor


def _serve_env(supervisor: BundleSupervisor, monkeypatch) -> tuple[str, dict[str, str]]:
    watched: list[str] = []

    def watch(name, url):
        watched.append(url)
        return False

    monkeypatch.setattr(supervisor, "_watch_child", watch)
    with mock.patch.object(bundle_supervisor.subprocess, "Popen", return_value=_child(701)) as popen:
        supervisor._ensure_ollama()
    assert popen.call_count == 1
    return watched[0], dict(popen.call_args.kwargs["env"])


def _api_env(supervisor: BundleSupervisor, tmp_path: Path) -> dict[str, str]:
    with mock.patch.object(bundle_supervisor.subprocess, "Popen", return_value=_child(702)) as popen:
        supervisor._start("api", ["python.exe", "vool_api_server.py"], cwd=tmp_path)
    assert popen.call_count == 1
    return dict(popen.call_args.kwargs["env"])


def test_serve_listens_on_loopback_only_and_keeps_no_inherited_origins(tmp_path, monkeypatch) -> None:
    supervisor = _supervisor(tmp_path, monkeypatch, INHERITED)
    watched, env = _serve_env(supervisor, monkeypatch)
    assert env["OLLAMA_HOST"] == "127.0.0.1:11434"
    assert not any(name.upper() == "OLLAMA_ORIGINS" for name in env)
    assert env["OLLAMA_NO_CLOUD"] == "1"
    assert watched == "http://127.0.0.1:11434/api/tags"


def test_the_api_child_gets_the_same_pinned_address_and_no_other(tmp_path, monkeypatch) -> None:
    supervisor = _supervisor(tmp_path, monkeypatch, INHERITED)
    env = _api_env(supervisor, tmp_path)
    assert env["VOOL_BUNDLE_OLLAMA_URL"] == "http://127.0.0.1:11434"
    assert env["VOOL_RAW_OLLAMA_API_URL"] == "http://127.0.0.1:11434"
    assert env["OLLAMA_HOST"] == "127.0.0.1:11434"
    for name in ("VOOL_OLLAMA_URL", "VOOL_OLLAMA_CHAT_URL", "VOOL_OLLAMA_PS_URL", "OLLAMA_ORIGINS"):
        assert name not in env


def test_vools_client_talks_to_the_pin_whatever_else_is_inherited(tmp_path, monkeypatch) -> None:
    from core import intent_arbiter, system_resources
    from core.ollama_endpoint import ollama_api_url, ollama_base_url
    from installer import provider_probe

    supervisor = _supervisor(tmp_path, monkeypatch, INHERITED)
    child_env = _api_env(supervisor, tmp_path)
    for name, value in INHERITED.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("VOOL_BUNDLE_OLLAMA_URL", child_env["VOOL_BUNDLE_OLLAMA_URL"])
    assert ollama_base_url() == "http://127.0.0.1:11434"
    # Even a caller's mapping that names another endpoint cannot split it.
    assert ollama_base_url({"OLLAMA_HOST": "0.0.0.0:11434", "VOOL_RAW_OLLAMA_API_URL": "http://10.0.0.9:1"}) == (
        "http://127.0.0.1:11434"
    )
    assert ollama_api_url("/api/tags") == "http://127.0.0.1:11434/api/tags"
    assert intent_arbiter._ollama_chat_url() == "http://127.0.0.1:11434/api/chat"
    assert provider_probe._ollama_api_url() == "http://127.0.0.1:11434"
    seen: list[str] = []
    monkeypatch.setattr(system_resources, "is_loopback_url", lambda url: seen.append(url) or False)
    system_resources._ollama_loaded_bytes()
    assert seen == ["http://127.0.0.1:11434/api/ps"]


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"OLLAMA_HOST": "0.0.0.0:11434"}, "http://0.0.0.0:11434"),
        ({"OLLAMA_HOST": "127.0.0.1:12345"}, "http://127.0.0.1:12345"),
        ({"VOOL_OLLAMA_URL": "http://127.0.0.1:2222"}, "http://127.0.0.1:2222"),
        ({}, "http://127.0.0.1:11434"),
    ],
)
def test_without_the_supervisor_the_client_resolves_as_before(monkeypatch, env, expected) -> None:
    from core.ollama_endpoint import ollama_base_url

    for name in ("VOOL_BUNDLE_OLLAMA_URL", "VOOL_RAW_OLLAMA_API_URL", "OLLAMA_HOST", "VOOL_OLLAMA_URL"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert ollama_base_url() == expected


def test_without_the_supervisor_the_chat_and_ps_overrides_still_apply(monkeypatch) -> None:
    from core import intent_arbiter

    monkeypatch.delenv("VOOL_BUNDLE_OLLAMA_URL", raising=False)
    monkeypatch.setenv("VOOL_OLLAMA_CHAT_URL", "http://127.0.0.1:3333/api/chat")
    assert intent_arbiter._ollama_chat_url() == "http://127.0.0.1:3333/api/chat"
