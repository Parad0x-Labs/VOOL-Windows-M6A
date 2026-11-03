"""Text I/O that ruff PLW1514 flagged reads and writes UTF-8 whatever the host locale is.

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


def test_mutation_runner_edits_keep_a_utf8_source_byte_exact(tmp_path):
    source = tmp_path / "guard.py"
    source.write_bytes(f'NOTE = "{TEXT}"\nGUARD = True\n'.encode())
    _run_child("""
        import sys
        from pathlib import Path
        from ops.phase_r_multiintent_mutations import apply_edits
        apply_edits(Path(sys.argv[1]), [("GUARD = True", "GUARD = False")])
        print("null")
    """, str(source))
    assert source.read_bytes() == f'NOTE = "{TEXT}"\nGUARD = False\n'.encode()


def test_linux_bundle_refuses_a_foreign_desktop_entry_with_non_ascii_names(tmp_path):
    desktop = tmp_path / "vool.desktop"
    desktop.write_bytes(f"[Desktop Entry]\nName=other\nName[ja]={TEXT}\n".encode())
    got = _run_child("""
        import importlib.util, json, sys
        from pathlib import Path
        spec = importlib.util.spec_from_file_location("install_bundle", "installer/bundle/linux/install_bundle.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        tmp = Path(sys.argv[1])
        try:
            mod.check_entrypoints(tmp / "root", tmp / "no-binary", Path(sys.argv[2]))
        except ValueError as exc:
            print(json.dumps([type(exc).__name__, str(exc)]))
        else:
            print(json.dumps(None))
    """, str(tmp_path), str(desktop))
    assert got == ["ValueError", "Refusing to replace an unrelated desktop entry"]

