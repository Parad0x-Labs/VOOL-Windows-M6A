"""A durable Deny closes its original waiting card, never a newer run."""
from __future__ import annotations

import pytest

from tests.chat_page_js_harness import DOM, run_node, script


def denial_case(case: str) -> dict:
    return run_node(DOM + script() + """
const caseName = CASE_NAME;
const sid = 'openclaw:' + 'd'.repeat(20);
setDisplayedChat(sid);
const owner = chatState(sid);
const original = adoptRun(sid, newRun(sid));
original.turnId = 'original-turn';
original.permission = true;
original.status = 'awaiting_approval';
original.ended = caseName !== 'live';
original.endSummary = 'Review the exact action below.';
original.endedAt = '2026-10-12T03:38:34Z';
original.responseCommit = null;
const approval = {approval_id: 'synthetic-denial', task_id: original.turnId, session_id: sid};
owner.pendingApproval = approval;
attachRunDom(original, new El('div'));
permBarEl.hidden = false;
const posted = [];
postMode = async (payload) => {
  posted.push(payload);
  if (caseName === 'failure') throw new Error('durable closure failed');
  if (caseName === 'newer') {
    const newer = adoptRun(sid, newRun(sid));
    newer.turnId = 'newer-turn';
    owner.pendingApproval = {approval_id: 'newer-approval', task_id: newer.turnId, session_id: sid};
  }
  if (caseName === 'offscreen') setDisplayedChat('openclaw:' + 'e'.repeat(20));
  const response = Object.assign({}, approval, {status: 'denied'});
  if (caseName === 'foreign-approval') response.approval_id = 'foreign';
  if (caseName === 'foreign-turn') response.task_id = 'foreign-turn';
  if (caseName === 'foreign-chat') response.session_id = 'foreign-chat';
  if (caseName === 'malformed') return {ok: true};
  if (caseName === 'not-accepted') return {ok: false, approval: response};
  return {ok: true, approval: response};
};
await resolvePermission('deny', 'once');
if (caseName === 'repeated') await resolvePermission('deny', 'once');
out({
  original: {status: original.status, ended: original.ended, permission: original.permission,
    endSummary: original.endSummary, responseCommit: original.responseCommit,
    title: original.refs.title.textContent, stage: original.refs.stage.textContent,
    waitingClass: original.card.classList.contains('perm')},
  current: {turnId: owner.run.turnId, status: owner.run.status, ended: owner.run.ended},
  pending: owner.pendingApproval && owner.pendingApproval.approval_id,
  barHidden: permBarEl.hidden,
  posted,
});
""".replace("CASE_NAME", repr(case)))


@pytest.mark.parametrize("case", ["ended", "live", "repeated"])
def test_successful_denial_closes_the_original_waiting_run(case):
    result = denial_case(case)
    assert result["errors"] == []
    assert result["original"]["status"] == "cancelled"
    assert result["original"]["ended"] is True
    assert result["original"]["permission"] is False
    assert result["original"]["endSummary"] == "Permission denied"
    assert result["original"]["responseCommit"] is None
    assert result["original"]["title"] == "Stopped"
    assert result["original"]["stage"] == "Permission denied"
    assert result["original"]["waitingClass"] is False
    assert result["pending"] is None and result["barHidden"] is True
    assert len(result["posted"]) == 1
    assert result["posted"][0]["op"] == "resolve_approval"


@pytest.mark.parametrize("case", ["failure", "foreign-approval", "foreign-turn", "foreign-chat", "malformed", "not-accepted"])
def test_failed_or_foreign_resolution_never_clears_the_waiting_card(case):
    result = denial_case(case)
    assert result["errors"] == []
    assert result["original"]["status"] == "awaiting_approval"
    assert result["original"]["permission"] is True
    assert result["original"]["title"] == "Waiting for your approval"
    assert result["pending"] == "synthetic-denial"
    assert result["barHidden"] is False
    assert len(result["posted"]) == 1


def test_newer_run_and_approval_are_not_changed_by_the_original_denial():
    result = denial_case("newer")
    assert result["errors"] == []
    assert result["original"]["status"] == "cancelled"
    assert result["current"] == {"turnId": "newer-turn", "status": "running", "ended": False}
    assert result["pending"] == "newer-approval"
    assert result["barHidden"] is False


def test_navigation_during_denial_never_hides_another_chats_prompt():
    result = denial_case("offscreen")
    assert result["errors"] == []
    assert result["original"]["status"] == "cancelled"
    assert result["pending"] is None
    assert result["barHidden"] is False
