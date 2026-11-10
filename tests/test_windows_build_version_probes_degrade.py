"""VOOL's own build-version probes, where git is refused: the version is unknown and nothing crashes.

Two places read the commit VOOL itself was built from by starting git on VOOL's program folder:
``core/web/api/runtime.py`` (``git_output`` / ``git_checkout_state``, reached from
``core.runtime_continuity._daemon_commit_sha`` when a runtime attempt is written) and
``core.runtime_execution_tools._daemon_commit_sha`` (the commit stamped on mutation Activity events).
Neither asks ``safe_git.git_refused_here()``; on Windows the process-wide spawn guard is what stops
them (core/safe_git.py, ``install_spawn_guard``). These rows prove that a refused probe degrades to an
unknown version (an empty commit) instead of raising.

The guard is an audit hook that cannot be removed once installed, so each row drives it in a child
interpreter: the Windows decision forced on (``safe_git._REFUSE_GIT``), the real guard installed, the
probe's cache cleared, then the probe called. The child also checks that the guard is live and that a
direct git start is refused, so an empty answer cannot come from git simply being absent.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from core import safe_git

REPO_ROOT = Path(__file__).resolve().parents[1]

_CHILD = """
import json, subprocess, sys
sys.path.insert(0, {root!r})
from core import safe_git
safe_git._REFUSE_GIT = True
safe_git.install_spawn_guard()
{probe}
try:
    subprocess.run(["git", "--version"], capture_output=True)
    direct = "ran"
except safe_git.GitRefusedOnWindows as exc:
    direct = str(exc)
print(json.dumps({{"live": safe_git.spawn_guard_live(), "direct": direct, "probe": probe}}))
"""


def _run_child(probe: str) -> dict:
    completed = subprocess.run(
        [sys.executable, "-c", _CHILD.format(root=str(REPO_ROOT), probe=probe)],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120,
    )
    assert completed.returncode == 0, completed.stderr  # the probe did not crash the process
    answer = json.loads(completed.stdout.strip().splitlines()[-1])
    assert answer["live"] is True, answer
    assert answer["direct"] == safe_git.GIT_REFUSED_TEXT, answer
    return answer["probe"]


def test_the_runtime_attempt_build_commit_is_unknown_when_git_is_refused() -> None:
    probe = _run_child(
        "from core import runtime_continuity\n"
        "from core.runtime_paths import PROJECT_ROOT\n"
        "from core.web.api.runtime import git_checkout_state, git_output\n"
        "runtime_continuity._daemon_sha_cache = None\n"
        "probe = {'output': git_output(PROJECT_ROOT, 'rev-parse', 'HEAD'),\n"
        "         'state': git_checkout_state(PROJECT_ROOT),\n"
        "         'sha': runtime_continuity._daemon_commit_sha()}\n"
    )
    assert probe["output"] == "", probe
    assert probe["state"] == {"valid": False, "branch": "", "commit": "", "commit_full": "", "dirty": False}, probe
    assert probe["sha"] == "", probe


def test_the_activity_event_build_commit_is_unknown_when_git_is_refused() -> None:
    probe = _run_child(
        "from core import runtime_execution_tools\n"
        "runtime_execution_tools._DAEMON_COMMIT_SHA_CACHE.clear()\n"
        "probe = {'sha': runtime_execution_tools._daemon_commit_sha()}\n"
    )
    assert probe["sha"] == "", probe
