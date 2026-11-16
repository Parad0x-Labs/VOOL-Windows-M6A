"""A host that can't build VOOL.app is refused before the build script runs git or writes anything.

installer/bundle/build_macos_app.sh used to probe git (rev-parse, status) and create its output folder
(default ./dist) before its macOS check, so a run on Windows or Linux started a bare git and left a
folder behind. The refusal now comes first. This drives the real script on every OS, macOS included:
BASH_ENV defines ``uname`` to answer Linux and ``git`` to record that it ran, so neither the host nor any
git config can change what the row sees.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from tests.platform_helpers import bash_executable, bash_path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "installer" / "bundle" / "build_macos_app.sh"


def _environment(tmp_path: Path, marker: Path) -> dict[str, str]:
    shims = tmp_path / "shims.bash"
    shims.write_text(
        "uname() { echo Linux; }\n"
        f"git() {{ echo \"$*\" >> '{bash_path(marker)}'; return 1; }}\n",
        encoding="utf-8", newline="\n",
    )
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith(("GIT_", "BASH_"))}
    home = tmp_path / "home"
    home.mkdir()
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_CONFIG_COUNT="0", GIT_CEILING_DIRECTORIES=str(tmp_path), VOOL_ALLOW_DIRTY_BUILD="1",
               BASH_ENV=bash_path(shims))
    return env


def _run(tmp_path: Path, *args: str) -> tuple[subprocess.CompletedProcess, Path, Path]:
    fixture = tmp_path / "fixture"
    staged = fixture / "installer" / "bundle" / "build_macos_app.sh"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(SCRIPT.read_bytes().replace(b"\r\n", b"\n"))  # a CRLF checkout must not fail it for show
    marker = tmp_path / "git-ran.txt"
    result = subprocess.run([bash_executable(), bash_path(staged), *(bash_path(arg) for arg in args)],
                            cwd=str(tmp_path), env=_environment(tmp_path, marker), capture_output=True, text=True,
                            timeout=120)
    return result, fixture, marker


def test_a_non_macos_host_is_refused_before_git_runs_or_the_output_folder_is_made(tmp_path: Path) -> None:
    out = tmp_path / "out"
    result, fixture, marker = _run(tmp_path, "--out", str(out))
    shown = result.stdout + result.stderr

    assert result.returncode != 0, shown
    assert "can only be built on macOS (got Linux)" in shown, shown
    assert not marker.exists(), "git ran before the platform refusal: " + marker.read_text(encoding="utf-8")
    assert not out.exists() and not (fixture / "dist").exists() and not (tmp_path / "dist").exists()


def test_the_default_output_folder_is_not_made_either(tmp_path: Path) -> None:
    result, fixture, marker = _run(tmp_path)

    assert result.returncode != 0 and "can only be built on macOS" in result.stdout + result.stderr
    assert not marker.exists() and not (fixture / "dist").exists()
