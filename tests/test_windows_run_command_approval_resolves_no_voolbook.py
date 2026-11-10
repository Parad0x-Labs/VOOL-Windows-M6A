"""Windows: a sandbox.run_command approval and its resume resolve no public name before opt-in.

Captest (build b58c5eb0, clean Windows 11 VM, fresh profile, no keys, cloud never on, per-row
pcaps): one A + AAAA query pair for voolbook.com per "Run sandbox.run_command" approval
(`python calc.py`, `python p.py`, `python hello.py` in a marked folder), NXDOMAIN, and no TCP or UDP
to any voolbook address after it. Ship rule M5: nothing goes online before the owner opts in.

The call chain: the approval card is an APPROVAL_REQUIRED turn result, and
`response.decorate_chat_response` attaches a Hive footer to that class
(`response_policy_visibility.should_attach_hive_footer`). `HiveActivityTracker.build_chat_footer`
fetched the watcher dashboard whose URL the shipped `config/meet_clusters/*/cluster_manifest.json`
names (`https://voolbook.com/api/dashboard`). The fetch door (`remote_fetch_policy.open_remote`)
asked Local Only whether the URL is local, and `auto_local_only_mode.endpoint_is_local` resolved
the name with `socket.getaddrinfo`; without Local Only the transport resolves it to dial.

Windows is simulated with the seam the Windows cloud gate uses
(`core.ollama_cloud_models.CLOUD_REFUSAL_ACTIVE`, read by `windows_cloud_gate.active()`), and the
approved-job rig's own seams (`job_runner._is_windows_platform`, `network_guard._IS_WINDOWS`). The
resolver is spied on at the socket (`getaddrinfo`, `gethostbyname`, `gethostbyname_ex`): a loopback
name goes to the real resolver, any other name is recorded and answered NXDOMAIN. Module-level
`urlopen` is replaced by a transport that resolves its host first, as the real one does, so a request
that gets past the door shows as a lookup instead of being stopped by the suite's urlopen guard.
"""

from __future__ import annotations

import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from core import hive_activity_tracker as hat
from core import mode_permission_policy as mpp
from core import ollama_cloud_models
from tests.test_windows_approved_python_job_runs_once import _Served, jobs, windows, workspace

_LOOPBACK = {"", "localhost", "127.0.0.1", "::1", "0.0.0.0"}


class _Resolver:
    """Every name VOOL asks the resolver for. Loopback resolves for real; anything else is NXDOMAIN."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.names: list[str] = []
        real_gai, real_ghbn, real_ghbn_ex = socket.getaddrinfo, socket.gethostbyname, socket.gethostbyname_ex

        def _note(host: Any) -> str:
            name = (host.decode() if isinstance(host, bytes) else str(host or "")).strip().lower().strip("[]")
            if name not in _LOOPBACK:
                self.names.append(name)
            return name

        def getaddrinfo(host, *args, **kwargs):
            if _note(host) not in _LOOPBACK:
                raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
            return real_gai(host, *args, **kwargs)

        def gethostbyname(host):
            if _note(host) not in _LOOPBACK:
                raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
            return real_ghbn(host)

        def gethostbyname_ex(host):
            if _note(host) not in _LOOPBACK:
                raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
            return real_ghbn_ex(host)

        def resolving_urlopen(request, *args, **kwargs):
            target = getattr(request, "full_url", request)
            host = urlsplit(str(target or "")).hostname or ""
            socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)  # the real transport's first step
            raise urllib.error.URLError(f"no network in this test (host {host!r})")

        monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
        monkeypatch.setattr(socket, "gethostbyname", gethostbyname)
        monkeypatch.setattr(socket, "gethostbyname_ex", gethostbyname_ex)
        monkeypatch.setattr(urllib.request, "urlopen", resolving_urlopen)


@pytest.fixture
def fresh_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """No watcher URL of the owner's own: the shipped cluster manifests are the only ones."""
    monkeypatch.delenv("VOOL_HIVE_WATCHER_URL", raising=False)
    monkeypatch.delenv("VOOL_HIVE_FOLLOWUPS_ENABLED", raising=False)


def _local_only(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    if on:
        from core import policy_engine

        monkeypatch.setattr(policy_engine, "local_only_mode", lambda *a, **k: True)


def _served(tmp_path, monkeypatch, workspace, jobs, *, cloud_seam: bool):
    # The seam is set BEFORE the agent (and its Hive tracker) is built, as on Windows.
    monkeypatch.setattr(ollama_cloud_models, "CLOUD_REFUSAL_ACTIVE", cloud_seam)
    return _Served(tmp_path, monkeypatch, workspace, jobs)


def _run_command_allow_resume(served, workspace: Path, script: str) -> str:
    (workspace / script).write_text("print(6 * 7)\n", encoding="utf-8")
    call = {"intent": "sandbox.run_command", "arguments": {"command": f"python {script}", "cwd": str(workspace)}}
    _first, _resumed, token, _turn = served.ask_allow_resume(f"run python {script}", call)
    return token


@pytest.fixture
def cleanup():
    from core import runtime_paths

    try:
        yield
    finally:
        mpp.reset_mode_permission_state()
        runtime_paths.configure_runtime_home(None)


@pytest.mark.parametrize("local_only", [False, True], ids=["cloud-off", "local-only"])
@pytest.mark.parametrize("script", ["calc.py", "p.py", "hello.py"])
def test_windows_run_command_allow_and_resume_resolve_no_public_name(
    tmp_path, monkeypatch, fresh_profile, windows, workspace, jobs, cleanup, local_only, script
) -> None:
    _local_only(monkeypatch, local_only)
    served = _served(tmp_path, monkeypatch, workspace, jobs, cloud_seam=True)
    resolver = _Resolver(monkeypatch)

    token = _run_command_allow_resume(served, workspace, script)

    assert len(served.jobs) == 1, served.jobs
    assert mpp._APPROVALS[token]["status"] == "consumed"
    assert resolver.names == [], f"public names resolved across the approval and resume: {resolver.names}"


@pytest.mark.parametrize("local_only", [False, True], ids=["cloud-off", "local-only"])
def test_off_windows_the_approval_card_still_asks_the_shipped_watcher(
    tmp_path, monkeypatch, fresh_profile, windows, workspace, jobs, cleanup, local_only
) -> None:
    """Old behaviour off Windows: the card's Hive footer still reaches for the manifest's watcher."""
    _local_only(monkeypatch, local_only)
    served = _served(tmp_path, monkeypatch, workspace, jobs, cloud_seam=False)
    resolver = _Resolver(monkeypatch)

    _run_command_allow_resume(served, workspace, "calc.py")

    assert len(served.jobs) == 1, served.jobs
    assert "voolbook.com" in resolver.names, resolver.names


@pytest.mark.parametrize(
    ("cloud_seam", "owner_url", "expected"),
    [
        (True, "", None),
        (True, "https://watch.example.test", "https://watch.example.test/api/dashboard"),
        (False, "", "https://voolbook.com/api/dashboard"),
    ],
    ids=["windows-fresh-profile", "windows-owner-opted-in", "off-windows-manifest"],
)
def test_the_watcher_config_reads_the_shipped_manifest_only_off_windows(
    monkeypatch, fresh_profile, cloud_seam, owner_url, expected
) -> None:
    monkeypatch.setattr(ollama_cloud_models, "CLOUD_REFUSAL_ACTIVE", cloud_seam)
    if owner_url:
        monkeypatch.setenv("VOOL_HIVE_WATCHER_URL", owner_url)

    config = hat.load_hive_activity_tracker_config()

    assert config.watcher_api_url == expected
    assert config.enabled is (expected is not None)


@pytest.mark.parametrize("local_only", [False, True], ids=["cloud-off", "local-only"])
def test_a_tracker_built_before_the_windows_rule_resolves_nothing(monkeypatch, fresh_profile, local_only) -> None:
    """A tracker already holding the shipped watcher URL, with the real transport, inside an effect
    scope (so the fetch door would run), resolves no name once the seam is on."""
    from core.effect_gateway import named_background_effect_scope

    _local_only(monkeypatch, local_only)
    tracker = hat.HiveActivityTracker(
        hat.HiveActivityTrackerConfig(enabled=True, watcher_api_url="https://voolbook.com/api/dashboard")
    )
    monkeypatch.setattr(ollama_cloud_models, "CLOUD_REFUSAL_ACTIVE", True)
    resolver = _Resolver(monkeypatch)

    with named_background_effect_scope("test.windows_hive_watcher"):
        with pytest.raises(Exception):
            tracker.fetch_dashboard()
        handled, details = tracker.maybe_handle_command_details("who is online", session_id="s-1")

    assert handled and details.get("watcher_status") in {"not_configured", "unreachable"}, details
    assert resolver.names == [], resolver.names


# --- the other public Hive door: the bridge VoolBook post sync sends through -------------------


def _bridge_off(sent: list[str]):
    """The bridge as a fresh profile builds it: public Hive off (VOOL_PUBLIC_HIVE_ENABLED unset), yet
    with the shipped sample seed as ``topic_target_url``, which ``agent_runtime.voolbook``'s post
    edit and delete sync send to without asking ``enabled()``."""
    from core.public_hive.bridge import PublicHiveBridge
    from core.public_hive.config import PublicHiveBridgeConfig

    seed = "https://seed-eu-1.example.vool"

    def urlopen(request, *args, **kwargs):
        target = getattr(request, "full_url", request)
        sent.append(str(target))
        socket.getaddrinfo(urlsplit(str(target)).hostname or "", 443)  # the real transport's first step
        raise urllib.error.URLError("no network in this test")

    config = PublicHiveBridgeConfig(enabled=False, meet_seed_urls=(seed,), topic_target_url=seed)
    return PublicHiveBridge(config, urlopen=urlopen), seed


@pytest.mark.parametrize(
    "route", ["/v1/voolbook/post/p-1/delete", "/v1/voolbook/post/p-1/edit"], ids=["delete-sync", "edit-sync"]
)
def test_windows_voolbook_post_sync_sends_nothing_while_public_hive_is_off(monkeypatch, route) -> None:
    from core.remote_fetch_policy import RemoteFetchRefusedError

    monkeypatch.setattr(ollama_cloud_models, "CLOUD_REFUSAL_ACTIVE", True)
    sent: list[str] = []
    bridge, seed = _bridge_off(sent)
    resolver = _Resolver(monkeypatch)

    with pytest.raises(RemoteFetchRefusedError):
        bridge._post_json(seed, route, {"voolbook_peer_id": "peer-1"})
    with pytest.raises(RemoteFetchRefusedError):
        bridge._get_json(seed, "/v1/voolbook/feed")

    assert sent == [] and resolver.names == [], (sent, resolver.names)


def test_off_windows_voolbook_post_sync_still_sends_while_public_hive_is_off(monkeypatch) -> None:
    monkeypatch.setattr(ollama_cloud_models, "CLOUD_REFUSAL_ACTIVE", False)
    sent: list[str] = []
    bridge, seed = _bridge_off(sent)
    resolver = _Resolver(monkeypatch)

    with pytest.raises(Exception):
        bridge._post_json(seed, "/v1/voolbook/post/p-1/delete", {"voolbook_peer_id": "peer-1"})

    assert sent and "seed-eu-1.example.vool" in resolver.names, (sent, resolver.names)
