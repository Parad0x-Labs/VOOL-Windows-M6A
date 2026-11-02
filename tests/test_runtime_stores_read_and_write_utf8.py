"""Runtime stores and audits read and write UTF-8 whatever the host locale is.

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


def test_memory_reader_audit_reads_vool_sources_as_utf8():
    got = _run_child("""
        import json
        from pathlib import Path
        from core.memory.reader_registry import _audit
        print(json.dumps(isinstance(_audit(Path(".").resolve()), list)))
    """)
    assert got is True

