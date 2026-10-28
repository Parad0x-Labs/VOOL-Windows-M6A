"""`worker_alive` must judge the WHOLE command line, not the terminal-width slice.

`ps -p <pid> -o args=` may cut the command line to the terminal width unless unlimited
width is asked for (`-ww`, accepted by both macOS ps and Linux procps; procps cuts to
COLUMNS even when its output is piped). A detached update worker runs as
`<bundled python> -m core.updater.worker <plan path>`, which easily passes 80 columns, so
a bare `ps` can report a prefix that no longer contains the plan marker and a LIVE worker
reads as dead, letting the updater start new work over a running transaction. Both cases below drive a fake ps
(ps itself does not exist on Windows): it answers with the command line cut to 80
characters unless `-ww` is in its argv, `_pid_has_exited` reports the pid alive, and a
matching `worker.json` lease is in place, so only the argv of ps differs from production.
"""
import json
import subprocess
from pathlib import Path

import core.updater.worker as worker_module
from core.updater.worker import worker_alive

#: A shipped-bundle command line: with the module and the plan appended, the plan marker
#: starts past column 84 on every platform, beyond the 80-column cut of a bare `ps`.
_INTERPRETER = "/Applications/VOOL.app/Contents/Resources/python/bin/python3"
_PID = 4242


class _FakePs:
    """Answers `ps -p <pid> -o args=` the way a narrow terminal does: the full line only
    when unlimited width was requested (`-ww`), else its first 80 columns."""

    WIDTH = 80

    def __init__(self, commands: dict[int, str]) -> None:
        self._commands = commands
        self.seen_argv: list[list[str]] = []

    def __call__(self, argv: list[str], **_kwargs):
        self.seen_argv.append(list(argv))
        line = self._commands.get(int(argv[argv.index("-p") + 1]), "")
        if "-ww" not in argv:
            line = line[: self.WIDTH]
        return subprocess.CompletedProcess(argv, 0 if line else 1, stdout=line, stderr="")


def _lease(data_dir: Path, plan: Path) -> None:
    root = data_dir / "update_v2"
    root.mkdir(parents=True, exist_ok=True)
    (root / "worker.json").write_text(
        json.dumps({"pid": _PID, "plan": str(plan), "txid": "tx-under-test"}), encoding="utf-8"
    )


def _worker_command(plan: Path) -> str:
    return f"{_INTERPRETER} -m core.updater.worker {plan}"


def test_live_worker_whose_plan_sits_past_the_width_cut_is_alive(tmp_path, monkeypatch):
    plan = tmp_path / "update_v2" / "workers" / ("tx" + "0123456789abcdef" * 4) / "plan.json"
    command = _worker_command(plan)
    assert command.index(str(plan)) > _FakePs.WIDTH, "the marker must sit past the cut"
    _lease(tmp_path, plan)
    monkeypatch.setattr(worker_module, "_pid_has_exited", lambda pid: False)
    monkeypatch.setattr(worker_module.subprocess, "run", _FakePs({_PID: command}))
    assert worker_alive(tmp_path) is True


def test_reused_pid_running_a_different_plan_is_not_alive(tmp_path, monkeypatch):
    plan = tmp_path / "update_v2" / "workers" / ("tx" + "0123456789abcdef" * 4) / "plan.json"
    other_plan = tmp_path / "update_v2" / "workers" / ("tx" + "ffffffffffffffff" * 4) / "plan.json"
    _lease(tmp_path, plan)
    monkeypatch.setattr(worker_module, "_pid_has_exited", lambda pid: False)
    monkeypatch.setattr(worker_module.subprocess, "run", _FakePs({_PID: _worker_command(other_plan)}))
    assert worker_alive(tmp_path) is False
