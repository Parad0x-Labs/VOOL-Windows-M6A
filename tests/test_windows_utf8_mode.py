"""The installed Windows app runs every bundled Python in UTF-8 mode.

Without it, Python on Windows decodes text as cp1252. About 80 runtime reads and writes name no
encoding, so a memory, note or file containing non-ASCII text fails or comes back corrupted. The
launcher sets PYTHONUTF8=1 before the first Python it starts, so the supervisor, API server and
window inherit it. The separately registered vool:// handler passes -X utf8 itself.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BUNDLE = REPO / "installer" / "bundle"


def test_the_launcher_enables_utf8_mode_before_starting_any_python() -> None:
    lines = (BUNDLE / "vool-launch.cmd").read_text(encoding="utf-8").splitlines()
    enable = next(i for i, line in enumerate(lines) if line.strip().lower() == 'set "pythonutf8=1"')
    first_python = next(i for i, line in enumerate(lines) if "python\\python" in line.lower())
    assert enable < first_python


def test_the_protocol_handler_runs_in_utf8_mode() -> None:
    iss = (BUNDLE / "vool.iss").read_text(encoding="utf-8")
    handler = next(line for line in iss.splitlines() if "vool_protocol_handler.py" in line and "shell\\open\\command" in line)
    assert '"" -X utf8 ""' in handler


def test_utf8_mode_round_trips_non_ascii_text_through_default_encoding(tmp_path: Path) -> None:
    sample = tmp_path / "note.txt"
    sample.write_bytes("Café ✓ 東京 — ok".encode())
    probe = (
        "import sys, pathlib; p = pathlib.Path(sys.argv[1]);"
        "t = p.read_text(); p.with_suffix('.out').write_text(t);"
        "print(sys.flags.utf8_mode)"
    )
    env = dict(os.environ, PYTHONUTF8="1")
    done = subprocess.run([sys.executable, "-c", probe, str(sample)], env=env, capture_output=True, text=True, check=True)
    assert done.stdout.strip() == "1"
    assert sample.with_suffix(".out").read_bytes() == sample.read_bytes()
