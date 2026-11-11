"""Served, real /api/chat: a model reply that says it ran a command, on a turn that ran nothing,
does not ship, and the turn's honesty receipt is not `no_action_claimed`.

Reproduction from the Windows claims sweep (build 170f6572, qwen3:8b): asked to run
`python -c "print(2+2)"`, the model answered "Let me run that... Output: `4` ✅" with no tool call.
Here a scripted loopback provider plays that model, certified through the real probe. Shared by
every OS; skips, never passes, when the rig cannot boot here.
"""
from __future__ import annotations

import json

FAKE_RUN_REPLY = "Let me run that... Output: `4` ✅"


def test_served_unbacked_run_is_replaced_and_receipted_as_blocked(tmp_path):
    from core.entity_ambiguity import AMBIGUITY_SYSTEM_PROMPT
    from tests import _reader_served_rig as rig

    def scripted_reply(body):
        if any(AMBIGUITY_SYSTEM_PROMPT in str(m.get("content", "")) for m in body.get("messages", [])):
            return json.dumps({"ambiguous": False, "referents": [], "clarification": ""})
        return FAKE_RUN_REPLY

    with rig.CapturingProvider(default=FAKE_RUN_REPLY, reply_fn=scripted_reply) as provider:
        daemon = rig.ServedDaemon(tmp_path / "home", provider=provider,
                                  env_extra={"VOOL_INSTALL_PROFILE": "hybrid-fallback"})
        try:
            daemon.start(timeout=120)
            certification = daemon.certify(timeout=120)
            assert certification.get("state") == "verified", certification
            session = rig.canonical_session("claimed-run")
            payload = daemon.chat(
                'what does python -c "print(2+2)" print? run it and show me the output',
                session_id=session, turn_id="claimed-run-turn",
            )
            commit = payload.get("vool_response_commit") or {}
            served = str(commit.get("canonical_content") or payload.get("response") or "")
            assert served, json.dumps(payload)[:800]
            assert "Output: `4`" not in served, served
            assert "Let me run that" not in served, served
            rows = [
                json.loads(line)
                for line in (daemon.home / "data" / "conversation_log.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            mine = [row for row in rows if row.get("session_id") == session]
            assert mine, "the served turn must be in the transcript"
            assert mine[-1]["assistant"] == served, (mine[-1]["assistant"][:200], served[:200])
            ledger = daemon.home / "data" / "honesty_receipts"
            receipts = [
                json.loads(line)
                for path in sorted(ledger.glob("*.jsonl"))
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ] if ledger.is_dir() else []
            assert receipts, "the served turn must sign an honesty receipt"
            assert receipts[-1]["verdict"] == "blocked_false_claim", receipts[-1]
            assert receipts[-1]["executed_tools"] == [], receipts[-1]
            assert all(r["verdict"] != "no_action_claimed" for r in receipts), receipts
            print("SERVED_CLAIMED_RUN", served[:400], receipts[-1]["verdict"], flush=True)
        finally:
            daemon.stop()
