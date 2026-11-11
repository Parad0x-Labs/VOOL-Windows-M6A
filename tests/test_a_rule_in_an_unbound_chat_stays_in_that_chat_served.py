"""Served proof: on a real daemon, a rule said in an unbound chat on the default folder reaches the model in that
chat only, its confirmation says "this chat", and a chat that named its folder keeps today's folder keying.

The provider records the exact request bodies, so "the model sees the rule" is read from what it was sent.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from tests._blackbox_served_rig import ServedDaemon, run_in_home
from tests._served_sufficiency_provider import SEED, BodyAwareScriptedProvider

REPO = Path(__file__).resolve().parents[1]
MODEL = "stub-chatrule:4b"
RULE = "Always end your answers with OK-TEST."
FOLDER_RULE = "From now on give me distances in kilometres."
PLAIN_TASK = "List three tips for writing clear emails."
# The rule as the standing block carries it ("- <text>"); the chat's own history repeats the owner's words as
# "User: ...", so only this line proves the rule itself reached the model.
RULE_LINE = "- " + RULE
FOLDER_RULE_LINE = "- " + FOLDER_RULE


class _Provider(BodyAwareScriptedProvider):
    def reply(self, model: str, body: dict[str, Any]) -> Any:
        if "well-known referents" in json.dumps(body):
            return '{"ambiguous": false, "referents": [], "clarification": ""}'
        return super().reply(model, body)


def _certify(daemon: ServedDaemon) -> dict[str, Any]:
    request = Request(f"{daemon.base_url}/api/model-tool-certification/run", method="POST",
                      data=json.dumps({"provider_name": "ollama-local", "model_name": MODEL,
                                       "timeout_seconds": 120}).encode(),
                      headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=400) as response:
        return json.loads(response.read().decode())


def _confirmations(payload: dict) -> list[str]:
    frame = payload.get("vool_profile") or {}
    return [str(item.get("report") or "") for item in frame.get("saved") or [] if isinstance(item, dict)]


def test_a_default_folder_chat_rule_reaches_only_that_chat_and_a_folder_rule_every_chat_there(tmp_path):
    home = tmp_path / "home"
    folder = tmp_path / "atlas"
    folder.mkdir()
    provider = _Provider({MODEL: "Keep it short, put the ask first, and use one topic per email."})
    with provider:
        run_in_home(home, SEED.format(root=REPO, base_url=provider.base_url, models=[(MODEL, {})]))
        with ServedDaemon(home, env_extra={"VOOL_MODEL_LOAD_FLOOR_GB": "0"}) as daemon:
            assert _certify(daemon).get("state") == "verified"

            def chat(text: str, session: str, **extra: Any) -> tuple[dict, str]:
                before = len(provider.calls)
                payload = daemon.chat(text, session_id=session, model=MODEL, timeout=600.0, **extra)
                sent = "\n".join(f"{c.get('system', '')}\n{c.get('full_prompt', '')}" for c in provider.calls[before:])
                return payload, sent

            chat_a, chat_b = "openclaw:" + "a" * 20, "openclaw:" + "b" * 20
            saved, _ = chat(RULE, chat_a)
            assert any(line.startswith("Saved for this chat:") and RULE in line for line in _confirmations(saved)), \
                _confirmations(saved)

            _, same_chat = chat(PLAIN_TASK, chat_a)
            assert RULE_LINE in same_chat
            _, new_chat = chat(PLAIN_TASK, chat_b)
            assert RULE_LINE not in new_chat

            # A chat that named its folder keeps the folder keying: every chat on that folder gets the rule.
            folder_a, folder_b = "openclaw:" + "c" * 20, "openclaw:" + "d" * 20
            saved_folder, _ = chat(FOLDER_RULE, folder_a, workspace=str(folder))
            assert any(line.startswith("Saved for every chat in this folder:") for line in _confirmations(saved_folder)), \
                _confirmations(saved_folder)
            _, other_folder_chat = chat(PLAIN_TASK, folder_b, workspace=str(folder))
            assert FOLDER_RULE_LINE in other_folder_chat and RULE_LINE not in other_folder_chat

            removed, _ = chat("Stop ending your answers with OK-TEST.", chat_a)
            assert any(RULE in line for line in _confirmations(removed)), _confirmations(removed)
            _, after = chat(PLAIN_TASK, chat_a)
            assert RULE_LINE not in after
