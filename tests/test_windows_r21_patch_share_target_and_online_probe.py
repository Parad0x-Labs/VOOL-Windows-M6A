"""Steve round 21 (train 8810bbf8, Windows): a patch naming a share is refused before any card, and "are you
online?" is never held by the entity-ambiguity probe.

1. A pasted patch whose `+++` named `\\\\localhost\\c$\\x.txt` raised an approval card ("Affects: ... (as the
   patch names it: it cannot be changed in ...)"). The patch tool refused it after the allow, but a card for
   a share is a card nobody should be asked. The patch lane now reads the diff's names (the patch tool's
   own list) with the path policy's spelling rules, nothing looked up, and refuses the whole patch in the
   words the other lanes use.
2. The first "are you online?" after a cold start got "I couldn't complete my check ... (tried 2x)": the
   ambiguity probe, which runs before the front door, timed out while the model loaded. The probe now leaves
   the online-status question to the front door's answer, as it does memory and identity asks.

Served through /api/chat with the fixtures of tests/test_windows_run_in_folder_served_end_to_end.py and both
Windows seams on (the write gate's and `windows_path_policy`'s); the fixture folders are POSIX temp folders,
so only the chat's own workspace checks are told they are local, as in
tests/test_windows_consistency_row6_patch_header_paths.py.
"""
from __future__ import annotations

import itertools
import uuid

import pytest

from core import windows_path_policy
from core import windows_write_gate as gate
from core.entity_ambiguity import single_plain_know_question
from tests.test_windows_consistency_row1_memory_ask_before_ambiguity_probe import probe
from tests.test_windows_run_in_folder_served_end_to_end import CALC, chat, jobs, machine

DIFF = "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a + b\n+    return b + a\n"


def _new_file(name: str) -> str:
    return f"--- /dev/null\n+++ {name}\n@@ -0,0 +1 @@\n+hello\n"


@pytest.fixture
def paths_on_windows(machine, monkeypatch):
    monkeypatch.setattr(windows_path_policy, "_IS_WINDOWS", True)
    monkeypatch.setattr(windows_path_policy, "chat_workspace_refusal", lambda *_a, **_k: None)
    monkeypatch.setattr(windows_path_policy, "context_workspace_refusal", lambda *_a, **_k: None)
    (machine["proj"] / "calc.py").write_bytes(CALC.encode("utf-8"))
    return machine


SHARES = {
    "localhost-admin-share": "\\\\localhost\\c$\\x.txt",
    "forward-slash-share": "//host/s/x",
    "unc-device": "\\\\?\\UNC\\host\\s\\x.txt",
    "pipe": "\\\\.\\pipe\\x",
    "b-prefixed-share": "b/\\\\host\\s\\x",
}


@pytest.mark.parametrize("case", list(SHARES))
@pytest.mark.parametrize("second", [False, True], ids=["only-file", "second-file"])
def test_a_patch_naming_a_share_is_refused_before_any_card(paths_on_windows, chat, case, second) -> None:
    proj = paths_on_windows["proj"]
    talk = chat(proj)
    patch = (DIFF if second else "") + _new_file(SHARES[case])

    reply = talk.say("apply this patch:\n" + patch, turn_id=f"r21-{uuid.uuid4().hex[:8]}")

    assert talk.pending() == [], reply
    assert talk.model_rounds == 0, reply
    assert "is a network or device path" in reply and "nothing was written" in reply, reply
    assert (proj / "calc.py").read_bytes() == CALC.encode("utf-8")


def test_a_share_only_in_the_old_side_header_is_refused_before_any_card(paths_on_windows, chat) -> None:
    """SA: the review list names only what the patch writes; a share in a modified file's `---` line is read too."""
    proj = paths_on_windows["proj"]
    talk = chat(proj)
    patch = DIFF.replace("--- a/calc.py", "--- \\\\h\\s\\x")

    reply = talk.say("apply this patch:\n" + patch, turn_id=f"r21-old-{uuid.uuid4().hex[:8]}")

    assert talk.pending() == [] and talk.model_rounds == 0, reply
    assert "is a network or device path" in reply and "nothing was written" in reply, reply
    assert (proj / "calc.py").read_bytes() == CALC.encode("utf-8")


def test_a_local_patch_still_gets_its_card(paths_on_windows, chat) -> None:
    proj = paths_on_windows["proj"]
    talk = chat(proj)

    card, reply = talk.ask_allow_resume("apply this patch:\n" + DIFF)

    assert card["intent"] == "workspace.apply_unified_diff", card
    assert (proj / "calc.py").read_text(encoding="utf-8") == CALC.replace("return a + b", "return b + a"), reply


def test_off_windows_the_patch_lane_reads_no_names(machine) -> None:
    from core import windows_marked_file_ops as marked

    assert not windows_path_policy.active()
    assert marked._shared_patch_target(_new_file(SHARES["localhost-admin-share"])) == ""


# --- 2: "are you online?" is not the probe's -------------------------------------------------------------

ONLINE_ASKS = ["are you online?", "are you offline", "are you connected to the internet?", "is web lookup on?"]


@pytest.mark.parametrize("ask", ONLINE_ASKS)
def test_online_asks_are_not_probe_eligible_on_windows(machine, ask) -> None:
    assert not single_plain_know_question(ask)


def test_a_cold_start_online_ask_is_answered_with_a_probe_that_times_out(machine, chat, probe) -> None:
    def timed_out(_verdict: str) -> str:
        raise TimeoutError("PROVIDER_TIMEOUT while the model loads")

    probe["replies"] = itertools.cycle([timed_out])
    talk = chat(machine["desktop"])

    reply = talk.say("are you online?", turn_id="r21-online")

    assert probe["calls"] == [], reply
    assert "couldn't complete my check" not in reply, reply
    assert "Web lookup:" in reply and talk.model_rounds == 0, reply


def test_off_windows_the_online_ask_keeps_its_eligibility(monkeypatch) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    before = single_plain_know_question("are you online?")
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    assert not single_plain_know_question("are you online?")
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    assert single_plain_know_question("are you online?") == before
