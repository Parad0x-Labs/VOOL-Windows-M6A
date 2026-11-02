"""Release and census tools read VOOL's own UTF-8 files whatever the host locale is.

Each case runs in a child Python with UTF-8 mode off and a C locale, so the locale encoding
is ASCII on POSIX and the ANSI code page on Windows. TEXT holds characters outside both ASCII
and cp1252 (and the byte 0x81, which cp1252 cannot decode)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEXT = "gold — spot → 日本 Á"


def _child_env() -> dict[str, str]:
    env = {
        "LC_ALL": "C",
        "LANG": "C",
        "PYTHONCOERCECLOCALE": "0",
        "PYTHONPATH": str(REPO_ROOT),
    }
    for key in ("PATH", "SYSTEMROOT", "SystemDrive", "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def _run_child(script: str, *args: str) -> object:
    proc = subprocess.run(
        [sys.executable, "-X", "utf8=0", "-c", textwrap.dedent(script), *args],
        capture_output=True, env=_child_env(), cwd=str(REPO_ROOT), timeout=120,
    )
    stderr = proc.stderr.decode("utf-8", "replace")
    assert proc.returncode == 0, f"child failed ({proc.returncode}):\n{stderr}"
    return json.loads(proc.stdout.decode("ascii"))


def test_command_census_reads_vool_sources_as_utf8():
    got = _run_child("""
        import json
        import core.command_registry.census as census
        print(json.dumps([len(census.discover_cli()) > 0, len(census.discover_http()) > 0,
                          len(census.discover_chat()) > 0]))
    """)
    assert got == [True, True, True]


def test_update_payload_reads_the_version_file_as_utf8(tmp_path):
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "app_version.py").write_bytes(
        f'# {TEXT}\nVOOL_VERSION = "9.9.9"\n'.encode())
    got = _run_child("""
        import json, sys
        from pathlib import Path
        from installer.build_update_payload import _validate_source
        try:
            _validate_source(Path(sys.argv[1]), "1.0.0")
        except ValueError as exc:
            print(json.dumps([type(exc).__name__, str(exc)]))
    """, str(tmp_path))
    assert got == ["ValueError", "runtime version '9.9.9' does not match '1.0.0'"]
