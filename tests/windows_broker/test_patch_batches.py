"""Patches through the Windows broker as one batch (docs/WINDOWS_BROKER.md sections 6, 6.1 and 8,
Phase C slice C3).

On Windows, workspace.apply_unified_diff is rendered by VOOL's own engine from the bytes it checked.
Once the API has started the broker, the rendered files are written by one `apply_batch`: every
`expect` (the hash of the bytes the patch was rendered from, or `absent` for a new file) is checked
before any file changes, and a batch that stops part way is put back by the broker's journal. A
refused or restored batch is a refusal with nothing written; a batch the broker could not put back is
the client's BatchNotRestored, never a plain refusal. Without a started broker, the pinned writer is
used as before.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any

import pytest

from core import windows_write_gate
from core.execution.workspace_tools import apply_unified_diff_workspace
from core.windows_broker import session
from core.windows_broker.process import FlaggedFolder
from core.windows_broker.protocol import BatchNotRestored, BatchRestored, BrokerRefusal, EffectOutcomeUnknown

from .test_single_writes import FOLDER_ID, _Broker, _wire


class _BatchBroker(_Broker):
    def request(self, operation: str, **arguments: Any) -> dict[str, Any]:
        if operation != "apply_batch":
            return super().request(operation, **arguments)
        self.requests.append((operation, arguments))
        if operation in self.fail:
            raise self.fail[operation]
        results = []
        for op in arguments["ops"]:
            assert op["op"] == "write_file" and set(op) == {"op", "rel", "data", "expect"}
            sha = hashlib.sha256(base64.b64decode(op["data"])).hexdigest()
            results.append({"op": "write_file", "path": op["rel"], "file_id": "f" * 32, "sha256": sha})
        return {"ok": True, "batch_id": "b3", "results": results, "cleanup_left": []}


@pytest.fixture
def flagged(tmp_path: Path) -> Path:
    folder = tmp_path / "flagged"
    folder.mkdir()
    (folder / "a.txt").write_bytes(b"one\ntwo\n")
    (folder / "b.txt").write_bytes(b"red\n")
    return folder.resolve()


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setattr(windows_write_gate, "_IS_WINDOWS", True)
    monkeypatch.setattr(session, "_host", None)
    return monkeypatch


TWO_FILES = """--- a/a.txt
+++ b/a.txt
@@ -1,2 +1,2 @@
 one
-two
+TWO
--- a/b.txt
+++ b/b.txt
@@ -1 +1 @@
-red
+green
"""

NEW_FILE = """--- /dev/null
+++ b/new/c.txt
@@ -0,0 +1 @@
+made
"""


def _apply(flagged: Path, patch: str) -> dict[str, Any]:
    return apply_unified_diff_workspace({"patch": patch}, workspace_root=flagged, session_id="s1")


def _ops(broker: _Broker) -> list[dict[str, Any]]:
    return broker.last("apply_batch")["ops"]


def test_a_patch_is_written_as_one_batch_with_the_hashes_it_was_rendered_from(windows: pytest.MonkeyPatch, flagged: Path) -> None:
    broker = _BatchBroker(flagged)
    _wire(windows, broker)
    result = _apply(flagged, TWO_FILES)
    assert result["ok"] and result["status"] == "executed", result["response_text"]
    assert result["details"]["changed_paths"] == ["a.txt", "b.txt"]
    assert broker.ops() == ["flagged_path", "open_root", "apply_batch"]
    ops = _ops(broker)
    assert [op["rel"] for op in ops] == ["a.txt", "b.txt"]
    assert ops[0]["expect"] == "sha256:" + hashlib.sha256(b"one\ntwo\n").hexdigest()
    assert ops[1]["expect"] == "sha256:" + hashlib.sha256(b"red\n").hexdigest()
    assert base64.b64decode(ops[0]["data"]) == b"one\nTWO\n" and base64.b64decode(ops[1]["data"]) == b"green\n"
    assert (flagged / "a.txt").read_bytes() == b"one\ntwo\n"  # nothing by path
    assert broker.closed


def test_a_new_file_gets_its_folder_first_and_expect_absent(windows: pytest.MonkeyPatch, flagged: Path) -> None:
    broker = _BatchBroker(flagged)
    _wire(windows, broker)
    result = _apply(flagged, NEW_FILE)
    assert result["ok"], result["response_text"]
    assert broker.ops() == ["flagged_path", "open_root", "ensure_directory", "apply_batch"]
    assert broker.last("ensure_directory")["rel"] == "new"
    assert _ops(broker) == [{"op": "write_file", "rel": "new\\c.txt", "data": base64.b64encode(b"made\n").decode(),
                             "expect": "absent"}]
    assert not (flagged / "new").exists()


@pytest.mark.parametrize("error", [BrokerRefusal("changed_since_check", "changed"),
                                   BatchRestored("io_error", "disk", {"batch_id": "b", "ran": 1, "restored": True})])
def test_a_refused_or_restored_batch_writes_nothing(windows: pytest.MonkeyPatch, flagged: Path, error: Exception) -> None:
    broker = _BatchBroker(flagged, fail={"apply_batch": error})
    _wire(windows, broker)
    result = _apply(flagged, TWO_FILES)
    assert not result["ok"] and result["status"] == windows_write_gate.STATUS
    assert result["details"]["broker_code"] == error.code  # type: ignore[attr-defined]
    assert result["details"]["changed_paths"] == []
    assert "nothing was written" in result["response_text"].lower()


def test_a_batch_the_broker_could_not_put_back_is_outcome_unknown(windows: pytest.MonkeyPatch, flagged: Path) -> None:
    error = BatchNotRestored("restore_incomplete", "left", "io_error", {"batch_id": "b", "ran": 1, "restored": False, "left": ["a.txt"]})
    broker = _BatchBroker(flagged, fail={"apply_batch": error})
    _wire(windows, broker)
    with pytest.raises(EffectOutcomeUnknown):
        _apply(flagged, TWO_FILES)


def test_files_in_two_flagged_folders_are_refused_before_anything_changes(windows: pytest.MonkeyPatch, flagged: Path) -> None:
    inner = flagged / "inner"
    inner.mkdir()
    (inner / "x.txt").write_bytes(b"x\n")
    broker = _BatchBroker(flagged)
    _wire(windows, broker, FlaggedFolder(FOLDER_ID, str(flagged)), FlaggedFolder("i" * 48, str(inner)))
    patch = TWO_FILES + "--- a/inner/x.txt\n+++ b/inner/x.txt\n@@ -1 +1 @@\n-x\n+y\n"
    result = _apply(flagged, patch)
    assert not result["ok"] and result["details"]["broker_code"] == "spans_flagged"
    assert broker.requests == []


def test_a_patch_that_changes_nothing_sends_nothing(windows: pytest.MonkeyPatch, flagged: Path) -> None:
    broker = _BatchBroker(flagged)
    _wire(windows, broker)
    same = "--- a/b.txt\n+++ b/b.txt\n@@ -1 +1 @@\n-red\n+red\n"
    result = _apply(flagged, same)
    assert result["status"] == "no_change"
    assert broker.requests == []


def test_without_a_started_broker_the_pinned_writer_is_used(windows: pytest.MonkeyPatch, flagged: Path) -> None:
    result = _apply(flagged, TWO_FILES)
    assert result["ok"], result["response_text"]
    assert (flagged / "b.txt").read_bytes() == b"green\n"
