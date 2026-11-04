"""provider_probe's Ollama children cannot hang the probe through inherited output handles.

On Windows ``ollama list`` / ``ollama run`` start the Ollama server when it is not running, and
that long-lived grandchild inherits whatever stdout/stderr the CLI was given. With
``subprocess.run(capture_output=True, timeout=N)`` the timeout kills only the CLI, and on
Windows CPython then reads the pipes again with no timeout, so the probe waited for the
server to exit (seen on Steve, 4 Nov 2026: ``provider_probe.py --json`` stuck for hours).
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time

import pytest

from installer import provider_probe

_PARENT_WITH_LINGERING_CHILD = textwrap.dedent(
    """
    import subprocess, sys, time
    # The stand-in server: inherits this process's stdout/stderr and outlives it.
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], close_fds=False)
    print("cli started", flush=True)
    time.sleep({cli_sleep})
    """
)


def _stand_in_cli(tmp_path, cli_sleep: float) -> list[str]:
    script = tmp_path / "stand_in_cli.py"
    script.write_text(_PARENT_WITH_LINGERING_CHILD.format(cli_sleep=cli_sleep), encoding="utf-8")
    return [sys.executable, str(script)]


def test_a_timed_out_child_with_a_lingering_grandchild_returns_within_its_bound(tmp_path) -> None:
    argv = _stand_in_cli(tmp_path, cli_sleep=60)
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        provider_probe._run_bounded_child(argv, timeout=5)
    elapsed = time.monotonic() - started
    # The grandchild sleeps 30 s holding the inherited handles; the bound must not wait for it.
    assert elapsed < 20, f"the probe waited {elapsed:.1f}s for a grandchild it does not own"
    assert "cli started" in str(caught.value.stdout or "")


def test_a_finished_child_with_a_lingering_grandchild_returns_its_output_promptly(tmp_path) -> None:
    argv = _stand_in_cli(tmp_path, cli_sleep=0)
    started = time.monotonic()
    completed = provider_probe._run_bounded_child(argv, timeout=20)
    elapsed = time.monotonic() - started
    assert elapsed < 15, f"the probe waited {elapsed:.1f}s for a grandchild it does not own"
    assert completed.returncode == 0
    assert "cli started" in completed.stdout


def test_ollama_list_and_run_go_through_the_bounded_runner(monkeypatch) -> None:
    calls: list[tuple[list[str], float]] = []

    def fake_runner(argv, *, timeout):
        calls.append((list(argv), timeout))
        if argv[1] == "list":
            stdout = "NAME    ID    SIZE    MODIFIED\nqwen:7b    abc123    4.1 GB    2 days ago\n"
        else:
            stdout = provider_probe.BENCHMARK_MARKER
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setattr(provider_probe, "_run_bounded_child", fake_runner)
    monkeypatch.setattr(provider_probe, "_list_ollama_models_via_api", lambda *_a, **_k: [])
    monkeypatch.setattr(provider_probe, "_list_ollama_models_via_manifests", lambda *_a, **_k: [])

    def no_direct_run(*_a, **_k):
        raise AssertionError("an Ollama call bypassed the bounded runner")

    monkeypatch.setattr(provider_probe.subprocess, "run", no_direct_run)

    rows = provider_probe.list_ollama_models(ollama_binary="ollama-stand-in")
    result = provider_probe.run_ollama_benchmark(
        model_name="qwen:7b", ollama_binary="ollama-stand-in", timeout_seconds=7
    )

    assert [c[0][1] for c in calls] == ["list", "run"]
    assert calls[0][1] == 15 and calls[1][1] == 7
    assert rows and rows[0]["name"] == "qwen:7b"
    assert result["status"] == "ok"


def test_a_benchmark_timeout_is_still_reported_as_a_timeout(monkeypatch) -> None:
    def timing_out(argv, *, timeout):
        raise subprocess.TimeoutExpired(argv, timeout, output="partial", stderr="")

    monkeypatch.setattr(provider_probe, "_run_bounded_child", timing_out)
    result = provider_probe.run_ollama_benchmark(
        model_name="qwen:7b", ollama_binary="ollama-stand-in", timeout_seconds=3
    )
    assert result["status"] == "timeout"
    assert result["output_excerpt"] == "partial"
