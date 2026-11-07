"""Attack pack for the Windows confinement backend (docs/WINDOWS_CONFINEMENT.md; plan chunk 6).

Every test drives the real door, `JobRunner.run`, with a confined Python child that tries one
escape and reports what happened. A test passes only when the child demonstrably RAN (it prints
the marker first) AND the escape failed, with the outside world checked from this process too. A
refused job is NOT a pass here: until the backend works these tests are red on purpose. They are
the acceptance tests for chunks 1-3, and any one of them passing an escape is RED for shipping.

Windows only: every attack is a Windows path, handle or ACL behaviour. Each one targets files and
endpoints this test creates under its own tmp_path, never the operator's real data.
"""

from __future__ import annotations

import ctypes
import json
import os
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows confinement backend")

if os.name == "nt":
    from sandbox.job_runner import JobRunner
    from sandbox.resource_limits import ExecutionPolicy

RAN = "VOOL-ATTACK-RAN"

#: The child's harness: prints RAN, loads its parameters from attack.json in the workspace (so no
#: outside path ever appears in argv, where the static guard would stop it before the kernel is
#: tested), runs the attack body, and prints ESCAPED or BLOCKED:<error>.
_HARNESS = """
import json, os, sys
print({ran!r}, flush=True)
P = json.load(open("attack.json", encoding="utf-8"))
try:
{body}
    print("ESCAPED", flush=True)
except Exception as exc:
    print("BLOCKED:" + type(exc).__name__ + ":" + str(exc)[:200], flush=True)
"""


def _run(ws: Path, body: str, params: dict | None = None, **policy):
    (ws / "attack.json").write_text(json.dumps(params or {}), encoding="utf-8")
    code = _HARNESS.format(ran=RAN, body=textwrap.indent(textwrap.dedent(body).strip(), "    "))
    runner = JobRunner(ExecutionPolicy(workspace_root=ws, **policy))
    return runner.run([sys.executable, "-c", code], cwd=ws)


def _assert_ran_and_blocked(result) -> None:
    assert RAN in result.stdout, f"the confined child never ran (refused is not a pass): {result.stderr[-400:]}"
    assert "ESCAPED" not in result.stdout, f"escape succeeded: {result.stdout[-400:]}"
    assert "BLOCKED:" in result.stdout


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    path = tmp_path / "ws"
    path.mkdir()
    return path


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    path = tmp_path / "outside"
    path.mkdir()
    (path / "secret.txt").write_text("original", encoding="utf-8")
    return path


def _unchanged(outside: Path) -> None:
    assert (outside / "secret.txt").read_text(encoding="utf-8") == "original"
    assert sorted(p.name for p in outside.iterdir()) == ["secret.txt"]


# ---- positive control: the backend confines AND still lets a job work in its workspace ---------


def test_control_a_confined_job_runs_and_writes_inside_its_workspace(ws: Path) -> None:
    result = _run(ws, 'open("inside.txt", "w").write("ok")')
    assert RAN in result.stdout and "ESCAPED" in result.stdout, result.stderr[-400:]
    assert result.returncode == 0
    assert (ws / "inside.txt").read_text() == "ok"


# ---- writes outside the workspace --------------------------------------------------------------


@pytest.mark.parametrize(
    "spelling",
    ["absolute", "dotdot", "extended", "forward_slashes", "short_name", "ads_on_outside_file"],
)
def test_no_spelling_of_an_outside_path_is_writable(ws: Path, outside: Path, spelling: str) -> None:
    target = outside / "secret.txt"
    if spelling == "absolute":
        path = str(target)
    elif spelling == "dotdot":
        path = os.path.join("..", "outside", "secret.txt")
    elif spelling == "extended":
        path = "\\\\?\\" + str(target)
    elif spelling == "forward_slashes":
        path = str(target).replace("\\", "/")
    elif spelling == "short_name":
        buf = ctypes.create_unicode_buffer(1024)
        ctypes.windll.kernel32.GetShortPathNameW(str(target), buf, 1024)
        path = buf.value or str(target)  # a volume without 8.3 names still gets the long spelling
    else:
        path = str(target) + ":hidden"
    result = _run(ws, 'open(P["path"], "w").write("pwned")', {"path": path})
    _assert_ran_and_blocked(result)
    _unchanged(outside)


def test_a_new_file_cannot_be_created_outside(ws: Path, outside: Path) -> None:
    result = _run(ws, 'open(P["path"], "x").write("pwned")', {"path": str(outside / "new.txt")})
    _assert_ran_and_blocked(result)
    _unchanged(outside)


def test_a_junction_inside_the_workspace_does_not_open_the_folder_it_points_at(ws: Path, outside: Path) -> None:
    subprocess.run(["cmd", "/c", "mklink", "/J", str(ws / "j"), str(outside)], check=True, capture_output=True)
    result = _run(ws, 'open(os.path.join("j", "secret.txt"), "w").write("pwned")')
    _assert_ran_and_blocked(result)
    _unchanged(outside)


def test_the_job_cannot_make_its_own_link_out_of_the_workspace(ws: Path, outside: Path) -> None:
    body = """
    import subprocess
    made = []
    for kind, args in (("hardlink", ["/H"]), ("symlink", []), ("junction", ["/J"])):
        name = "l_" + kind
        target = P["file"] if kind != "junction" else P["dir"]
        subprocess.run(["cmd", "/c", "mklink", *args, name, target], capture_output=True)
        if os.path.lexists(name):
            made.append(name)
    for name in made:
        path = name if os.path.isfile(name) else os.path.join(name, "secret.txt")
        try:
            open(path, "w").write("pwned")
        except OSError:
            continue
        raise SystemExit(print("ESCAPED") or 0)
    raise PermissionError("no link reached the outside file")
    """
    result = _run(ws, body, {"file": str(outside / "secret.txt"), "dir": str(outside)})
    _assert_ran_and_blocked(result)
    _unchanged(outside)


def test_one_jobs_workspace_is_never_writable_by_another_job(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    first = _run(a, 'open("mine.txt", "w").write("a")')  # gives workspace A its grants
    assert "ESCAPED" in first.stdout and (a / "mine.txt").exists()
    result = _run(b, 'open(P["path"], "w").write("pwned by b")', {"path": str(a / "mine.txt")})
    _assert_ran_and_blocked(result)
    assert (a / "mine.txt").read_text() == "a"


# ---- repository metadata ----------------------------------------------------------------------


def test_an_existing_git_directory_is_not_writable(ws: Path) -> None:
    hooks = ws / ".git" / "hooks"
    hooks.mkdir(parents=True)
    (ws / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    result = _run(ws, 'open(os.path.join(".git", "hooks", "pre-commit"), "w").write("evil")')
    _assert_ran_and_blocked(result)
    assert not (hooks / "pre-commit").exists()


_PLANT = """
root = os.path.join("nested", ".git") if P["layout"] == "dot_git" else "planted.git"
for sub in ("objects", "refs", "hooks"):
    os.makedirs(os.path.join(root, sub), exist_ok=True)
open(os.path.join(root, "HEAD"), "w").write("ref: refs/heads/main\\n")
open(os.path.join(root, "config"), "w").write("[core]\\n\\tbare = true\\n\\tfsmonitor = calc.exe\\n")
"""


def _refused_for_the_repository(ws: Path, body: str, params: dict, **policy) -> None:
    from sandbox.job_runner import NestedGitMetadataError

    try:
        result = _run(ws, body, params, **policy)
    except NestedGitMetadataError:
        return  # the result was refused: the pass
    raise AssertionError(f"a job that planted a repository was not refused (exit {result.returncode})")


@pytest.mark.parametrize("layout", ["dot_git", "bare"])
def test_a_repository_planted_during_the_job_fails_the_job(ws: Path, layout: str) -> None:
    body = _PLANT + 'raise PermissionError("planted; the launcher must refuse the result")\n'
    _refused_for_the_repository(ws, body, {"layout": layout})


@pytest.mark.parametrize("layout", ["dot_git", "bare"])
def test_a_repository_planted_by_a_job_that_then_hangs_is_still_refused(ws: Path, layout: str) -> None:
    # The runner's timeout kills the launcher before its own post-job scan runs, so the runner must
    # scan (the launcher's `scan` form) before it returns anything. Otherwise a job could plant a
    # repository and simply never exit.
    body = _PLANT + "import time\ntime.sleep(600)\n"
    _refused_for_the_repository(ws, body, {"layout": layout}, max_seconds=5)
    # And the next job on the same workspace is refused too: grant must not "heal" the planted layout.
    from sandbox.job_runner import KernelIsolationUnavailableError, NestedGitMetadataError

    with pytest.raises((KernelIsolationUnavailableError, NestedGitMetadataError)):
        _run(ws, 'open("after.txt", "w").write("x")')
    assert not (ws / "after.txt").exists()


# ---- reads -------------------------------------------------------------------------------------


def test_files_outside_the_workspace_and_read_roots_are_unreadable(ws: Path, outside: Path) -> None:
    result = _run(ws, 'print("LEAK:" + open(P["path"]).read())', {"path": str(outside / "secret.txt")})
    _assert_ran_and_blocked(result)
    assert "original" not in result.stdout


def test_the_runtime_state_folder_is_neither_readable_nor_writable(ws: Path) -> None:
    from core.runtime_paths import active_vool_home

    home = Path(active_vool_home())
    home.mkdir(parents=True, exist_ok=True)
    probe = home / "confinement-attack-probe.txt"
    probe.write_text("state", encoding="utf-8")
    try:
        body = """
        try:
            print("LEAK:" + open(P["read"]).read())
        except OSError:
            pass
        open(P["write"], "w").write("pwned")
        """
        result = _run(ws, body, {"read": str(probe), "write": str(home / "planted.txt")})
        _assert_ran_and_blocked(result)
        assert "LEAK:" not in result.stdout
        assert not (home / "planted.txt").exists()
    finally:
        probe.unlink(missing_ok=True)


def test_the_real_user_profile_is_neither_listable_nor_the_jobs_home(ws: Path) -> None:
    # Tool config and credentials (.npmrc, pip.ini, .cargo\credentials.toml, .git-credentials) live
    # under the real profile. The job must not see it, and its own profile variables must point
    # into its workspace, so a tool that reads USERPROFILE/APPDATA never lands on real files.
    # Each must be INSIDE the job's workspace: a prefix check against the real profile proves
    # nothing when the workspace itself lives under the profile (CI's %TEMP%, a user's Documents).
    real = os.environ["USERPROFILE"]
    body = """
    import os
    inside = os.path.normcase(os.path.abspath(P["ws"])) + os.sep
    for name in ("USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        value = os.environ.get(name, "")
        if not os.path.normcase(os.path.abspath(value or os.sep)).startswith(inside):
            print("REALHOME:" + name + "=" + value, flush=True)
    os.listdir(P["real"])
    """
    result = _run(ws, body, {"real": real, "ws": str(ws)})
    _assert_ran_and_blocked(result)
    assert "REALHOME:" not in result.stdout


def test_a_batch_file_is_never_started_as_the_program(ws: Path) -> None:
    # CreateProcess on a .cmd/.bat runs cmd.exe implicitly (the BatBadBut argument-injection
    # class). The launcher refuses it; here refused IS the pass, and the script must not have run.
    script = ws / "x.cmd"
    script.write_text("@echo off\r\necho ran> ran.txt\r\n", encoding="utf-8")
    runner = JobRunner(ExecutionPolicy(workspace_root=ws))
    try:
        result = runner.run([str(script)], cwd=ws)
    except Exception:  # refused before launch (KernelIsolationUnavailableError or a guard)
        result = None
    assert not (ws / "ran.txt").exists()
    if result is not None:
        assert result.returncode != 0


# ---- network and other processes --------------------------------------------------------------


def test_loopback_is_unreachable_even_to_a_listener_of_this_process(ws: Path) -> None:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    accepted: list[bool] = []

    def _accept() -> None:
        server.settimeout(15)
        try:
            server.accept()
            accepted.append(True)
        except OSError:
            pass

    thread = threading.Thread(target=_accept, daemon=True)
    thread.start()
    try:
        body = """
        import socket
        socket.create_connection(("127.0.0.1", P["port"]), timeout=5).close()
        """
        result = _run(ws, body, {"port": server.getsockname()[1]})
        _assert_ran_and_blocked(result)
    finally:
        server.close()
        thread.join(timeout=20)
    assert accepted == []


def test_the_internet_is_unreachable_when_the_policy_denies_network(ws: Path) -> None:
    body = """
    import socket
    socket.create_connection(("1.1.1.1", 443), timeout=5).close()
    """
    result = _run(ws, body)
    _assert_ran_and_blocked(result)


def test_a_named_pipe_of_this_user_cannot_be_opened(ws: Path) -> None:
    name = rf"\\.\pipe\vool-attack-{os.getpid()}-{time.monotonic_ns()}"
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateNamedPipeW.restype = ctypes.c_void_p
    pipe = kernel32.CreateNamedPipeW(name, 3, 0, 1, 4096, 4096, 0, None)  # duplex, default DACL
    assert pipe not in (None, ctypes.c_void_p(-1).value)
    try:
        result = _run(ws, 'open(P["pipe"], "r+b")', {"pipe": name})
        _assert_ran_and_blocked(result)
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(pipe))


def test_the_job_cannot_open_the_process_that_started_it(ws: Path) -> None:
    body = """
    import ctypes
    handle = ctypes.windll.kernel32.OpenProcess(0x0001 | 0x0010 | 0x0020, False, P["pid"])  # terminate, vm read, vm write
    if not handle:
        raise PermissionError("OpenProcess refused")
    """
    result = _run(ws, body, {"pid": os.getpid()})
    _assert_ran_and_blocked(result)


# ---- the job object ----------------------------------------------------------------------------


def _alive(pid: int) -> bool:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def test_a_grandchild_cannot_break_away_from_the_job(ws: Path) -> None:
    body = """
    import subprocess
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], creationflags=0x01000000)
    print("PID:%d" % child.pid, flush=True)
    """
    result = _run(ws, body)
    assert RAN in result.stdout, result.stderr[-400:]
    pids = [int(line[4:]) for line in result.stdout.splitlines() if line.startswith("PID:")]
    time.sleep(2)
    assert not any(_alive(pid) for pid in pids), "a breakaway grandchild outlived its job"


def test_the_whole_tree_is_gone_after_a_timeout(ws: Path) -> None:
    body = """
    import subprocess, time
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    print("PID:%d" % child.pid, flush=True)
    time.sleep(120)
    """
    started = time.monotonic()
    result = _run(ws, body, max_seconds=5)
    assert time.monotonic() - started < 60
    assert RAN in result.stdout, result.stderr[-400:]
    assert result.returncode != 0
    pids = [int(line[4:]) for line in result.stdout.splitlines() if line.startswith("PID:")]
    assert pids, "the child never started its grandchild"
    time.sleep(2)
    assert not any(_alive(pid) for pid in pids), "a grandchild outlived the timed-out job"


def test_the_memory_limit_ends_a_job_that_exceeds_it(ws: Path) -> None:
    body = """
    hog = bytearray(1024 * 1024 * 1024)  # 1 GiB, far above the 256 MB limit
    hog[::4096] = b"x" * len(hog[::4096])
    """
    result = _run(ws, body, max_memory_mb=256)
    assert RAN in result.stdout, result.stderr[-400:]
    assert "ESCAPED" not in result.stdout
    assert result.returncode != 0
