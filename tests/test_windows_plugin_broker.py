"""Live Windows checks for the native plugin broker.

These tests are intentionally opt-in: a Windows checkout without an explicitly published
broker remains fail-closed and should not silently launch a handler without confinement.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core.plugin_executor import run_plugin_tool

BROKER = str(os.environ.get("VOOL_WINDOWS_BROKER") or "").strip()
WINDOWS_BROKER = pytest.mark.skipif(
    os.name != "nt" or not BROKER,
    reason="requires Windows and an explicitly published VOOL Windows broker",
)


def _write_probe(plugin: Path, forbidden: Path, normal_profile: Path) -> None:
    script = plugin / "bin" / "probe.py"
    script.write_text(
        "import json, os, pathlib, socket\n"
        "root = pathlib.Path(__file__).resolve().parents[1]\n"
        "scratch = pathlib.Path(os.environ['PLUGIN_SCRATCH'])\n"
        f"forbidden = pathlib.Path({str(forbidden)!r})\n"
        f"normal_profile = pathlib.Path({str(normal_profile)!r})\n"
        "out = {'input': (root / 'allowed-input.txt').read_text(), 'intent': json.load(__import__('sys').stdin)['intent']}\n"
        "try:\n"
        "    (root / 'read-only-write.txt').write_text('blocked')\n"
        "    out['plugin_write_denied'] = False\n"
        "except OSError:\n"
        "    out['plugin_write_denied'] = True\n"
        "try:\n"
        "    out['forbidden_read_denied'] = forbidden.read_text() != 'canary'\n"
        "except OSError:\n"
        "    out['forbidden_read_denied'] = True\n"
        "try:\n"
        "    out['normal_profile_read_denied'] = normal_profile.read_text() != 'canary'\n"
        "except OSError:\n"
        "    out['normal_profile_read_denied'] = True\n"
        "try:\n"
        "    (scratch / 'allowed-write.txt').write_text('allowed')\n"
        "    out['scratch_write_allowed'] = True\n"
        "except OSError:\n"
        "    out['scratch_write_allowed'] = False\n"
        "def blocked(host, port):\n"
        "    try:\n"
        "        with socket.create_connection((host, port), timeout=1):\n"
        "            return False\n"
        "    except OSError:\n"
        "        return True\n"
        "out['loopback_denied'] = blocked('127.0.0.1', 1)\n"
        "out['external_denied'] = blocked('198.51.100.1', 80)\n"
        "print(json.dumps({'ok': True, 'observation': out}))\n",
        encoding="utf-8",
    )


@WINDOWS_BROKER
def test_brokered_plugin_has_scoped_filesystem_and_network_access(tmp_path: Path) -> None:
    plugin = tmp_path / "plugin"
    (plugin / "bin").mkdir(parents=True)
    (plugin / ".codex-plugin").mkdir()
    (plugin / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": "test-broker-plugin", "version": "1.0.0"}),
        encoding="utf-8",
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    forbidden_dir = tmp_path / "forbidden"
    forbidden_dir.mkdir()
    forbidden = forbidden_dir / "canary.txt"
    forbidden.write_text("canary", encoding="utf-8")
    normal_profile = tmp_path / "normal-profile-canary.txt"
    normal_profile.write_text("canary", encoding="utf-8")
    (plugin / "allowed-input.txt").write_text("allowed", encoding="utf-8")
    _write_probe(plugin, forbidden, normal_profile)

    handler = {"kind": "subprocess", "entry": "bin/probe.py", "timeout_seconds": 10}
    read_only = run_plugin_tool(
        intent="test-broker.probe",
        arguments={},
        handler=handler,
        schema={},
        plugin_root=plugin,
        scratch_dir=str(scratch),
        read_only=True,
    )
    assert read_only.ok, (read_only.status, read_only.error, read_only.as_details())
    assert read_only.confinement == "windows:appcontainer_job_object"
    assert read_only.observation["input"] == "allowed"
    assert read_only.observation["plugin_write_denied"] is True
    assert read_only.observation["forbidden_read_denied"] is True
    assert read_only.observation["normal_profile_read_denied"] is True
    assert read_only.observation["scratch_write_allowed"] is False
    assert read_only.observation["loopback_denied"] is True
    assert read_only.observation["external_denied"] is True
    assert read_only.confinement_evidence["job_owned"] is True
    assert read_only.confinement_evidence["network_policy"] == "none"
    assert {row["access"] for row in read_only.confinement_evidence["filesystem_grants"]} == {"read_only"}
    assert not (plugin / "read-only-write.txt").exists()
    assert not (scratch / "allowed-write.txt").exists()

    writable = run_plugin_tool(
        intent="test-broker.probe",
        arguments={},
        handler=handler,
        schema={},
        plugin_root=plugin,
        scratch_dir=str(scratch),
        read_only=False,
    )
    assert writable.ok, (writable.status, writable.error, writable.as_details())
    assert writable.observation["scratch_write_allowed"] is True
    assert writable.observation["forbidden_read_denied"] is True
    assert writable.observation["normal_profile_read_denied"] is True
    grants = {Path(row["path"]).resolve().name: row["access"] for row in writable.confinement_evidence["filesystem_grants"]}
    assert grants[plugin.name] == "read_only"
    assert grants[scratch.name] == "read_write"
    assert (scratch / "allowed-write.txt").read_text(encoding="utf-8") == "allowed"
    assert not (normal_profile / "unexpected.txt").exists()


@WINDOWS_BROKER
def test_broker_refuses_network_enabled_plugin_request(tmp_path: Path) -> None:
    plugin = tmp_path / "plugin"
    (plugin / "bin").mkdir(parents=True)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    script = plugin / "bin" / "probe.py"
    script.write_text("print('{}')\n", encoding="utf-8")
    result = run_plugin_tool(
        intent="test-broker.network",
        arguments={},
        handler={"kind": "subprocess", "entry": "bin/probe.py", "network": True},
        schema={},
        plugin_root=plugin,
        scratch_dir=str(scratch),
        allow_network=True,
    )
    assert not result.ok
    assert result.status == "confinement_unavailable"
    assert not result.confinement_evidence
