"""The bundled Ollama the Windows supervisor starts has Ollama's own online features turned off.

Capability test A6 (0.7.1-beta, clean Windows 11 VM, nothing turned on): about 3.5 minutes after the
VM's network came on, ``{app}\\ollama\\ollama.exe`` -- started by the bundle supervisor -- opened a
TCP connection to ollama.com. VOOL's own pulls are gated (the boot pull needs
``network.outbound_enabled``, a user pull needs a click), so the call came from Ollama itself, which
the supervisor started with Ollama's cloud features at their default (on). The supervisor now starts
it with ``OLLAMA_NO_CLOUD=1``, whatever the inherited environment says. A user-requested pull still
goes to Ollama's registry: that is a model download, not a cloud feature.
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

from installer.bundle import bundle_supervisor
from installer.bundle.bundle_supervisor import BundleSupervisor


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
    # Nothing answers on 11434, so _ensure_ollama starts the bundled server.
    monkeypatch.setattr(supervisor, "_watch_child", lambda name, url: False)
    return supervisor


def _started_env(supervisor: BundleSupervisor) -> tuple[list[str], dict[str, str]]:
    with mock.patch.object(bundle_supervisor.subprocess, "Popen", return_value=_child(701)) as popen:
        supervisor._ensure_ollama()
    assert popen.call_count == 1
    return list(popen.call_args.args[0]), dict(popen.call_args.kwargs["env"])


def test_the_bundled_ollama_starts_with_ollama_cloud_off(tmp_path, monkeypatch) -> None:
    supervisor = _supervisor(tmp_path, monkeypatch)
    argv, env = _started_env(supervisor)
    assert argv[-1] == "serve" and argv[0].endswith("ollama.exe")
    assert env.get("OLLAMA_NO_CLOUD") == "1"


def test_an_inherited_setting_cannot_turn_ollama_cloud_back_on(tmp_path, monkeypatch) -> None:
    supervisor = _supervisor(tmp_path, monkeypatch, {"OLLAMA_NO_CLOUD": "0"})
    _argv, env = _started_env(supervisor)
    assert env.get("OLLAMA_NO_CLOUD") == "1"


def test_the_offline_setting_is_for_ollama_and_keeps_its_model_store(tmp_path, monkeypatch) -> None:
    supervisor = _supervisor(tmp_path, monkeypatch)
    _argv, env = _started_env(supervisor)
    assert env.get("OLLAMA_MODELS") == supervisor.env["OLLAMA_MODELS"]
