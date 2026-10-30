"""Writer/reader pairs must agree on UTF-8 whatever the host locale is.

Each case runs in a child Python with UTF-8 mode off and a C locale, so the
locale encoding is ASCII on POSIX and the ANSI code page on Windows. TEXT holds
characters outside both ASCII and cp1252 (and the byte 0x81, which cp1252 cannot
decode), so a read or write that falls back to the locale fails on every host."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEXT = "gold — spot → 日本 Á"


def _child_env() -> dict[str, str]:
    env = {
        "LC_ALL": "C",
        "LANG": "C",
        "PYTHONCOERCECLOCALE": "0",
        "PYTHONPATH": str(REPO_ROOT),
    }
    for key in ("PATH", "SYSTEMROOT", "SystemDrive", "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def _run_child(script: str, *args: str) -> object:
    proc = subprocess.run(
        [sys.executable, "-X", "utf8=0", "-c", textwrap.dedent(script), *args],
        capture_output=True, env=_child_env(), cwd=str(REPO_ROOT), timeout=120,
    )
    stderr = proc.stderr.decode("utf-8", "replace")
    assert proc.returncode == 0, f"child failed ({proc.returncode}):\n{stderr}"
    return json.loads(proc.stdout.decode("ascii"))


def test_open_review_writes_non_ascii_turns_as_utf8(tmp_path):
    _run_child("""
        import json, sys
        from pathlib import Path
        import core.kernel.consensus as consensus
        text = json.loads(sys.argv[2])
        turns = [(text, "  ob1 [chat] x\\n\\nCOMMIT: committed\\n\\n\\x01" + text + "\\x02\\n", "01:00:00")]
        consensus.open_review(turns, "abc1234", Path(sys.argv[1]))
        print("null")
    """, str(tmp_path), json.dumps(TEXT))
    review = next(d for d in tmp_path.iterdir() if d.is_dir())
    assert TEXT in (review / "transcript.md").read_bytes().decode("utf-8")
    assert TEXT in (review / "FULL_REVIEW_CONTEXT.md").read_bytes().decode("utf-8")


def test_open_review_counts_a_utf8_journal_under_any_locale(tmp_path):
    journal = tmp_path / "repl_sessions.jsonl"
    rows = [{"run_id": "r1", "question": TEXT}, {"run_id": "r1", "question": TEXT}]
    journal.write_bytes("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode())
    bus = tmp_path / "bus"
    bus.mkdir()
    got = _run_child("""
        import json, sys
        from pathlib import Path
        import core.kernel.consensus as consensus
        turn = ("q", "  ob1 [chat] x\\n\\nCOMMIT: committed\\n", "01:00:00")
        try:
            consensus.open_review([turn], "abc1234", Path(sys.argv[1]), run_id="r1", journal_path=sys.argv[2])
        except consensus.FragmentedTranscriptError:
            print(json.dumps("fragmented"))
        else:
            print(json.dumps("opened"))
    """, str(bus), str(journal))
    assert got == "fragmented", "the journal holds 2 turns for r1, the transcript 1"


def test_verdict_and_print_round_read_a_utf8_vote(tmp_path):
    vote = tmp_path / "round_1_grok.md"
    vote.write_bytes(f"A. {TEXT}\nVERDICT: AGREE\n".encode())
    got = _run_child("""
        import io, json, sys
        from pathlib import Path
        import core.kernel.consensus as consensus
        path = Path(sys.argv[1])
        verdict = consensus._verdict(path)
        out, sys.stdout = sys.stdout, io.StringIO()
        consensus._print_round(path, "grok", 1)
        shown, sys.stdout = sys.stdout.getvalue(), out
        print(json.dumps({"verdict": verdict, "shown": shown}))
    """, str(vote))
    assert got["verdict"] == "AGREE"
    assert TEXT in got["shown"]


def test_consensus_record_keeps_the_holder_text_as_utf8(tmp_path):
    got = _run_child("""
        import io, json, sys, threading
        from pathlib import Path
        import core.kernel.consensus as consensus
        bus, text = Path(sys.argv[1]), json.loads(sys.argv[2])
        seats = ("kimi", "codex", "grok", "fable")
        block = ("=== CONSENSUS ===\\nROOT CAUSE: " + text + "\\nAGREED FIX: z\\n"
                 "RAW-EVIDENCE COMPLETENESS VERIFIED: YES\\n"
                 "KIMI: AGREE\\nCODEX: AGREE\\nGROK: AGREE\\nFABLE: AGREE\\n")
        def drive():
            review = next(d for d in bus.iterdir() if d.is_dir())
            for a in seats:
                (review / f"round_1_{a}.md").write_bytes(
                    ("A. coverage\\n" + block + "\\nVERDICT: AGREE\\n").encode())
        turns = [("q", "  ob1 [chat] x\\n\\nCOMMIT: committed\\n", "01:00:00")]
        t = threading.Timer(0.05, drive)
        out, sys.stdout = sys.stdout, io.StringIO()
        t.start()
        status = consensus.run_review(turns, "abc1234", bus, poll_s=0.02, max_wait_s=5,
                                      voting_seats=seats, contributors=())
        t.join()
        sys.stdout = out
        print(json.dumps(status))
    """, str(tmp_path), json.dumps(TEXT))
    assert got == "consensus"
    review = next(d for d in tmp_path.iterdir() if d.is_dir())
    assert "ROOT CAUSE: " + TEXT in (review / "consensus.md").read_bytes().decode("utf-8")


def test_liquefy_receipts_read_back_their_utf8_lines(tmp_path):
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    row = {"segment_id": "s1", "actual": TEXT}
    (receipts / "s1.corrupt.json").write_bytes(json.dumps(row, ensure_ascii=False).encode() + b"\n")
    (receipts / "restore.jsonl").write_bytes(json.dumps(row, ensure_ascii=False).encode() + b"\n")
    got = _run_child("""
        import json, sys
        from pathlib import Path
        from types import SimpleNamespace
        from core.liquefy import operator
        store = SimpleNamespace(receipts_dir=Path(sys.argv[1]))
        print(json.dumps({
            "rows": operator._receipt_lines(store, "corruption", tail=10),
            "count": operator._receipt_line_count(store, "restore.jsonl"),
        }))
    """, str(receipts))
    assert got["rows"] == [row]
    assert got["count"] == 1


def test_addon_snapshot_rewrites_the_directory_as_utf8(tmp_path):
    core = tmp_path / "core"
    core.mkdir()
    directory = {"schema": 2, "entries": [{"id": "x", "name": TEXT, "import_ready": True}]}
    (core / "addon_directory.json").write_bytes(json.dumps(directory, ensure_ascii=False).encode())
    _run_child("""
        import io, sys
        from pathlib import Path
        import tools.build_addon_snapshot as snap
        snap.__file__ = str(Path(sys.argv[1]) / "tools" / "build_addon_snapshot.py")
        snap.build = lambda d, catalog: (d, {"schema": 1, "files": {}})
        out, sys.stdout = sys.stdout, io.StringIO()
        snap.main()
        sys.stdout = out
        print("null")
    """, str(tmp_path))
    assert json.loads((core / "addon_directory.json").read_bytes().decode("utf-8")) == directory
