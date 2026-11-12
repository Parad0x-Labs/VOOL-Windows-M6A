"""Every open.log line from the window host starts with a UTC timestamp.

Hardware triage of the Setup folder bug had to line open.log ("setup window created" ... "setup
window closed") up against the API's request log by order alone. A UTC ISO-8601 time at the start
of each line makes that a lookup. Existing readers only search lines for substrings, so a prefix
keeps every one of them working.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone


def test_each_line_carries_a_utc_time(tmp_path, monkeypatch) -> None:
    from installer.bundle import vool_window as nw

    monkeypatch.setattr(nw, "_state_dir", lambda: str(tmp_path))
    before = datetime.now(timezone.utc).replace(microsecond=0)
    nw._log("setup window created")
    nw._log("two words\n")
    after = datetime.now(timezone.utc)
    lines = (tmp_path / "open.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for line, text in zip(lines, ("setup window created", "two words"), strict=True):
        match = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) (.*)", line)
        assert match, line
        assert match.group(2) == text
        stamp = datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        assert before - timedelta(seconds=1) <= stamp <= after


def test_a_log_failure_is_still_silent(monkeypatch) -> None:
    from installer.bundle import vool_window as nw

    monkeypatch.setattr(nw, "_state_dir", lambda: "/nonexistent/\x00bad")
    nw._log("nothing breaks")
