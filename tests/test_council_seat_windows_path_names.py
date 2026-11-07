"""A seat path Windows would rewrite or reject is refused by the fence, contained, before any write.

T34's Windows suite (run 41943131575) failed
test_shell_syntax_in_a_path_never_reaches_a_shell_and_never_escapes for "$(echo /tmp)/pwned.txt"
and "`echo /tmp`/pwned.txt": the fence let the path through (it does land in the jail) and the write
then failed with a bare 'error'. Both strings split into a component ending in a space ("$(echo ",
"`echo "), and Windows strips trailing spaces and dots, refuses <>:"|?* and reserves device names,
so the name on disk would not be the name the fence judged. On Windows the fence must refuse such a
path itself, with the containment status, and a plain name must still write.

The platform is driven through the fence's ``_IS_WINDOWS`` flag, so this runs on Linux too.
"""
from __future__ import annotations

import pytest

from tests.test_council_seat_no_shell_and_cancellation import (
    CONTAINED,
    SeatThatActs,
    _run,
    clean_containment,
    isolated_store,
    operator_checkout,
)


def _write(operator_checkout, path: str):
    seats = SeatThatActs("workspace.write_file", {"path": path, "content": "x\n"}, workspace_root=operator_checkout)
    run = _run(operator_checkout, seats, grants={"s1": ("workspace_write",)})
    run.run()
    return run, seats.executions[0]


@pytest.mark.parametrize(
    "raw",
    [
        "$(echo /tmp)/pwned.txt",
        "`echo /tmp`/pwned.txt",
        "notes /plan.txt",
        "notes./plan.txt",
        "plan.txt.",
        "a<b.txt",
        "a|b.txt",
        "a?b.txt",
        "a*b.txt",
        'a"b.txt',
        "plan.txt:stream",
        "CON/plan.txt",
        "nul.txt",
        "lpt1",
        "CONOUT$",
        "conin$.txt",
        "CLOCK$/plan.txt",
        "COM\u00b9",
        "LPT\u00b3.txt",
        "plan.txt ",
    ],
)
def test_a_name_windows_would_change_is_refused_and_contained(
    isolated_store, operator_checkout, clean_containment, monkeypatch, raw
):
    monkeypatch.setattr(clean_containment, "_IS_WINDOWS", True, raising=False)
    run, execution = _write(operator_checkout, raw)

    assert execution is not None and not execution.ok
    assert execution.status == CONTAINED, execution
    jail = clean_containment.seat_workspace(run.run_id, "s1")
    assert not [p for p in jail.rglob("*") if p.is_file()], "a refused write left a file"


def test_a_plain_name_still_writes_inside_the_jail(isolated_store, operator_checkout, clean_containment, monkeypatch):
    monkeypatch.setattr(clean_containment, "_IS_WINDOWS", True, raising=False)
    run, execution = _write(operator_checkout, "notes/plan-v2.txt")

    assert execution is not None and execution.ok, execution
    jail = clean_containment.seat_workspace(run.run_id, "s1")
    assert (jail / "notes" / "plan-v2.txt").read_text(encoding="utf-8") == "x\n"
