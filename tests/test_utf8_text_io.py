"""Subprocess and file text is UTF-8, never the locale code page.

Each check runs the code under test in a child Python whose locale encoding is ASCII
(LC_ALL=C, no locale coercion, UTF-8 mode off). Non-ASCII text fails there the way it fails
under cp1252 on Windows, so these fail on the unfixed code on Linux CI and on Windows alike.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEXT = "Jörg ✓ 日本"   # ö is cp1252; ✓ and 日本 are not


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


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), "-c", "core.quotepath=false", *args],
        check=True, capture_output=True, timeout=30,
    )


def _init_repo(root: Path) -> None:
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "n@x")
    _git(root, "config", "user.name", "Ninja")


def test_child_locale_is_not_utf8():
    # Guards the method itself: if the child were UTF-8 the checks below would prove nothing.
    got = _run_child("""
        import json, locale
        print(json.dumps(locale.getpreferredencoding(False)))
    """)
    assert str(got).lower().replace("-", "") not in {"utf8"}


def test_git_diff_reaches_the_user_as_utf8(tmp_path):
    root = tmp_path / "checkout"
    _init_repo(root)
    (root / "a.txt").write_bytes(b"base\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    (root / "a.txt").write_bytes(f"{TEXT}\n".encode())
    got = _run_child("""
        import json, sys
        from pathlib import Path
        from core.execution.git_tools import git_diff_workspace
        out = git_diff_workspace({}, workspace_root=Path(sys.argv[1]))
        print(json.dumps({"ok": out["ok"], "text": out["response_text"], "stdout": out["details"]["stdout"]}))
    """, str(root))
    assert got["ok"] is True
    assert TEXT in got["stdout"]
    assert TEXT in got["text"]


def test_run_git_decodes_commit_message_as_utf8(tmp_path):
    root = tmp_path / "checkout"
    _init_repo(root)
    (root / "a.txt").write_bytes(b"base\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", f"msg {TEXT}")
    got = _run_child("""
        import json, sys
        from pathlib import Path
        from core.execution.git_tools import _run_git
        out = _run_git(["log", "-1", "--format=%s"], cwd=Path(sys.argv[1]))
        print(json.dumps(out))
    """, str(root))
    assert got["status"] == "executed"
    assert got["stdout"].strip() == f"msg {TEXT}"


def _wsfs_script(body: str) -> str:
    return """
        import json, sys
        from core.platform.broker import ExecutionBroker, PlatformRevocations
        from core.repoops.identity import RepositoryWorkspace
        from core.repoops.wsfs import WorkspaceFS
        fs = WorkspaceFS(RepositoryWorkspace(root=sys.argv[1]))
        fork = PlatformRevocations().mint("builder-1", frozenset({"wsfs.read", "wsfs.write"}))
    """ + body


def test_wsfs_read_decodes_workspace_source_as_utf8(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "src.txt").write_bytes(TEXT.encode("utf-8"))
    got = _run_child(_wsfs_script("""
        print(json.dumps(fs.read("src.txt")))
    """), str(root))
    assert got == TEXT


def test_wsfs_write_is_utf8_and_agrees_with_the_byte_evidence(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    got = _run_child(_wsfs_script(f"""
        receipt = fs.write(fork, ExecutionBroker(), "out/src.txt", {TEXT!a})
        print(json.dumps({{"status": receipt.status, "evidence": dict(receipt.evidence or {{}})}}))
    """), str(root))
    expected = TEXT.encode("utf-8")
    assert got["status"] == "applied", got
    assert (root / "out" / "src.txt").read_bytes() == expected
    assert got["evidence"]["bytes"] == len(expected)


def test_local_subprocess_adapter_decodes_stdout_and_stderr_as_utf8():
    got = _run_child("""
        import json, sys
        from types import SimpleNamespace
        from adapters import local_subprocess_adapter as transport
        from adapters.base_adapter import ModelRequest

        transport.seal_provider_invocation = lambda **kw: SimpleNamespace(consume=lambda: kw["payload"])
        text = "J\\u00f6rg \\u2713 \\u65e5\\u672c"
        worker = (
            "import sys; sys.stdin.read(); "
            "sys.stdout.buffer.write(%s.encode('utf-8')); "
            "sys.stderr.buffer.write(%s.encode('utf-8')); "
            "sys.exit(%%d)" % (ascii(text), ascii(text))
        )
        def run(code):
            adapter = transport.LocalSubprocessAdapter(SimpleNamespace(
                provider_name="p", provider_id="p", model_name="m",
                runtime_config={"command": [sys.executable, "-c", worker % code], "timeout_seconds": 30.0},
            ))
            return adapter._invoke_with_env(ModelRequest(task_kind="chat", prompt="hi"), extra_env={})

        ok = run(0)
        try:
            run(3)
            failure = ""
        except RuntimeError as exc:
            failure = str(exc)
        print(json.dumps({"output": ok.output_text, "failure": failure}))
    """)
    assert got["output"] == TEXT
    assert TEXT in got["failure"]


def test_tracked_test_targets_decode_non_ascii_names_as_utf8(tmp_path):
    root = tmp_path / "checkout"
    _init_repo(root)
    name = "tests/test_jörg_日本.py"
    (root / "tests").mkdir()
    (root / name).write_bytes(b"def test_x():\n    pass\n")
    _git(root, "add", "-A")
    got = _run_child("""
        import json, sys
        from pathlib import Path
        from ops.pytest_shards import tracked_test_targets
        print(json.dumps(list(tracked_test_targets(Path(sys.argv[1])))))
    """, str(root))
    assert got == [name]
