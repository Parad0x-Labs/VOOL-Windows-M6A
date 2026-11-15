"""The API's abandon and the supervisor's start are one exclusive claim (K4 11, SA REBASE).

With two separate checks, the API could read no lease, the supervisor could pass its last check and
start the worker, and the API could then write its abandon marker and report "nothing was installed"
while the worker installed. Now both sides create the same file with O_CREAT|O_EXCL: the supervisor
before it starts the worker ("started"), the API when it stops waiting ("abandoned"). Whoever creates
it decides. An exclusive create does not follow a link planted at that name.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core.updater import worker as worker_module
from core.updater.windows import consume_worker_request
from core.updater.worker import (
    CLAIM_ABANDONED,
    CLAIM_STARTED,
    START_CLAIM_NAME,
    SupervisorStartedWorker,
    claim_transaction,
    request_supervisor_start,
    transaction_abandoned,
)
from tests.updater.test_worker_plan_and_abandon import TXID, _installed_bundle, _prepared_job, _RecordingPopen


def _ticking():
    """A clock that moves a second per read, so a wait loop ends quickly."""
    state = {"now": 0.0}

    def clock() -> float:
        state["now"] += 1.0
        return state["now"]

    return clock


def test_only_the_first_claim_wins(tmp_path) -> None:
    assert claim_transaction(tmp_path, CLAIM_STARTED, TXID) is True
    assert claim_transaction(tmp_path, CLAIM_ABANDONED, TXID) is False
    claim = json.loads((tmp_path / START_CLAIM_NAME).read_text(encoding="utf-8"))
    assert claim["outcome"] == CLAIM_STARTED and claim["txid"] == TXID


def test_when_the_supervisor_won_the_api_waits_for_the_lease_and_never_says_nothing_ran(tmp_path) -> None:
    home = tmp_path / "home"
    plan = _prepared_job(home, _installed_bundle(tmp_path))
    updater_root = home / "data" / "update_v2"
    assert claim_transaction(plan.parent, CLAIM_STARTED, TXID)
    reads = {"count": 0}

    def late_lease(_seconds) -> None:
        reads["count"] += 1
        if reads["count"] == 3:  # the supervisor publishes its lease during the API's grace wait
            (updater_root / "worker.json").write_text(
                json.dumps({"txid": TXID, "launcher": "supervisor", "pid": 4242, "plan": str(plan)}), encoding="utf-8")

    handle = request_supervisor_start(updater_root, TXID, plan, clock=_ticking(), sleeper=late_lease, timeout=2.0)
    assert isinstance(handle, SupervisorStartedWorker) and handle.pid == 4242


def test_when_the_supervisor_won_but_published_no_lease_the_api_does_not_claim_nothing_ran(tmp_path) -> None:
    home = tmp_path / "home"
    plan = _prepared_job(home, _installed_bundle(tmp_path))
    assert claim_transaction(plan.parent, CLAIM_STARTED, TXID)
    with pytest.raises(RuntimeError) as raised:
        request_supervisor_start(home / "data" / "update_v2", TXID, plan, clock=_ticking(),
                                 sleeper=lambda _s: None, timeout=1.0)
    assert "nothing was installed" not in str(raised.value)
    assert "check the update status" in str(raised.value)


def test_when_the_api_won_the_supervisor_never_starts_the_worker(tmp_path) -> None:
    home = tmp_path / "home"
    app_root = _installed_bundle(tmp_path)
    plan = _prepared_job(home, app_root)
    assert claim_transaction(plan.parent, CLAIM_ABANDONED, TXID)
    popen = _RecordingPopen()
    assert consume_worker_request(home, app_root, popen=popen, copytree=lambda *_a, **_k: None, now=lambda: 1.0) is None
    assert popen.calls == []


def test_a_link_planted_at_the_claim_is_not_followed_and_nothing_starts(tmp_path) -> None:
    home = tmp_path / "home"
    app_root = _installed_bundle(tmp_path)
    plan = _prepared_job(home, app_root)
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text("user file\n", encoding="utf-8")
    try:
        os.symlink(elsewhere, plan.parent / START_CLAIM_NAME)
    except OSError:
        # No symlink privilege (Windows without developer mode): a folder in its place, also not a claim.
        (plan.parent / START_CLAIM_NAME).mkdir()
    popen = _RecordingPopen()
    assert consume_worker_request(home, app_root, popen=popen, copytree=lambda *_a, **_k: None, now=lambda: 1.0) is None
    assert popen.calls == []
    assert transaction_abandoned(plan.parent) is True
    assert elsewhere.read_text(encoding="utf-8") == "user file\n"


@pytest.mark.parametrize(("content", "abandoned"), [
    (None, False),
    ({"outcome": "started"}, False),
    ({"outcome": "abandoned"}, True),
    ("not json", True),
    ({"outcome": "something else"}, True),
])
def test_the_worker_reads_the_claim_failing_closed(tmp_path, content, abandoned) -> None:
    if content is not None:
        text = content if isinstance(content, str) else json.dumps(content)
        (tmp_path / START_CLAIM_NAME).write_text(text, encoding="utf-8")
    assert transaction_abandoned(tmp_path) is abandoned


def test_a_claim_that_cannot_be_made_refuses_on_both_sides(tmp_path) -> None:
    missing = tmp_path / "no-such-job"
    with pytest.raises(OSError):
        claim_transaction(missing, CLAIM_STARTED, TXID)
    home = tmp_path / "home"
    plan = _prepared_job(home, _installed_bundle(tmp_path))
    gone = Path(str(plan.parent) + "-gone")
    plan.parent.rename(gone)
    with pytest.raises(RuntimeError, match="could not be recorded"):
        request_supervisor_start(home / "data" / "update_v2", TXID, plan, clock=_ticking(),
                                 sleeper=lambda _s: None, timeout=1.0)


def test_sabotage_without_the_supervisors_claim_an_abandoned_request_starts_a_worker(tmp_path, monkeypatch) -> None:
    """The control: if the supervisor's claim always succeeded, the API's abandon would not stop it."""
    home = tmp_path / "home"
    app_root = _installed_bundle(tmp_path)
    plan = _prepared_job(home, app_root)
    assert claim_transaction(plan.parent, CLAIM_ABANDONED, TXID)
    monkeypatch.setattr(worker_module, "claim_transaction", lambda job, outcome, txid: True)
    monkeypatch.setattr(worker_module, "transaction_abandoned", lambda job: False)
    popen = _RecordingPopen()
    assert consume_worker_request(home, app_root, popen=popen, copytree=lambda *_a, **_k: None, now=lambda: 1.0)
    assert len(popen.calls) == 1
