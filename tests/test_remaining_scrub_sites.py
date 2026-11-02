"""The remaining path scrub and display sites must run the shared Windows-aware recogniser too.

Each site below scrubbed or flagged local paths with POSIX-only or single-backslash patterns, so
an escaped path (doubled backslashes, as a repr or JSON shows it), a spaced account name, a
forward-slash drive path or a UNC share kept the account name in the site's output. Every site
keeps its own marker and its own existing patterns; these tests pin that the new spellings are
removed too and that what each site removed before is still removed. The inputs are plain
strings, so the result is the same on every host.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

# ``C:\\Users\\alice\\x\\f.py`` with the backslashes doubled in the text itself.
DOUBLED = r"C:\\Users\\alice\\x\\f.py"
SPACED = r"C:\Users\John Smith\x\f.py"
FORWARD_SPACED = "C:/Users/John Smith/x/f.py"
UNC = r"\\fileserver\share\alice\f.py"
ACCOUNT_WORDS = ("alice", "John", "Smith")


def _assert_no_account(out: str) -> None:
    for word in ACCOUNT_WORDS:
        assert word not in out, out


# --- core/proof_projection.py: _redact_text, marker "~" --------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (DOUBLED, r"read ~\\x\\f.py failed at 10:30"),
        (SPACED, r"read ~\x\f.py failed at 10:30"),
        (UNC, "read ~ failed at 10:30"),
    ],
)
def test_proof_projection_masks_windows_home_spellings(path: str, expected: str) -> None:
    from core.proof_projection import _redact_text, _redact_tree

    out = _redact_text(f"read {path} failed at 10:30")
    _assert_no_account(out)
    assert out == expected
    _assert_no_account(json.dumps(_redact_tree({"status": [path]})))


def test_proof_projection_still_masks_its_posix_home_input() -> None:
    from core.proof_projection import _redact_text

    out = _redact_text(f"ok for /Users/example-user/secret-notes.txt and {DOUBLED}")
    assert "/Users/example-user" not in out
    assert "example-user" not in out
    _assert_no_account(out)


# --- core/local_model_tool_certification.py: _redact_text, marker "[home]" --------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (DOUBLED, r"load [home]\\x\\f.py at 10:30"),
        (SPACED, r"load [home]\x\f.py at 10:30"),
        (UNC, "load [home] at 10:30"),
    ],
)
def test_tool_certification_masks_windows_home_spellings(path: str, expected: str) -> None:
    from core.local_model_tool_certification import _redact_text

    out = _redact_text(f"load {path} at 10:30")
    _assert_no_account(out)
    assert out == expected


def test_tool_certification_still_masks_its_existing_input() -> None:
    from core.local_model_tool_certification import _redact_text

    out = _redact_text(
        "request timed out Bearer super-secret token=abc123 /Users/alice/private/model.gguf "
        + SPACED
    )
    assert "super-secret" not in out
    assert "abc123" not in out
    assert "/Users/alice" not in out
    assert "[redacted]" in out
    assert "[home]" in out
    _assert_no_account(out)


# --- core/fact_extractor.py: _redact_sensitive_text, marker "[REDACTED]" ----------------------


@pytest.mark.parametrize("path", [DOUBLED, SPACED, FORWARD_SPACED, UNC])
def test_fact_extractor_redacts_windows_paths(path: str) -> None:
    from core.fact_extractor import _redact_sensitive_text

    out = _redact_sensitive_text(f"log at {path} then retry")
    _assert_no_account(out)
    assert out == "log at [REDACTED] then retry"


def test_fact_extractor_macos_home_class_means_whitespace() -> None:
    # The pattern was written r"/Users/[^\\s]+": inside a raw string that class excludes a
    # backslash and the letter "s", not whitespace. So the match stopped at the first "s" of the
    # path (leaving "sktop/notes.txt") and ran on through spaces into the prose.
    from core.fact_extractor import _redact_sensitive_text

    assert _redact_sensitive_text("notes at /Users/bob/Desktop/notes.txt are ready") == (
        "notes at [REDACTED] are ready"
    )
    assert _redact_sensitive_text("at /Users/bob/Dev now open") == "at [REDACTED] now open"


def test_fact_extractor_still_redacts_its_existing_inputs() -> None:
    from core.fact_extractor import _redact_sensitive_text

    out = _redact_sensitive_text(
        "logs at /home/alice/private/log.txt, root key /root/.ssh/id_rsa here, "
        f"at /Users/bob/Desktop/secret.txt and {SPACED}"
    )
    assert "[REDACTED]" in out
    assert "/home/alice" not in out
    assert ".ssh" not in out
    assert "bob" not in out
    assert "secret.txt" not in out
    _assert_no_account(out)


# --- ops/run_local_acceptance.py: _sanitize_text, marker "/Users/<redacted>" ------------------


@pytest.mark.parametrize("path", [DOUBLED, SPACED, FORWARD_SPACED])
def test_local_acceptance_masks_windows_home_spellings(path: str, tmp_path: Path) -> None:
    from ops import run_local_acceptance

    out = run_local_acceptance._sanitize_text(f"cmd {path} ok", repo_root=tmp_path / "repo")
    _assert_no_account(out)
    assert out.startswith("cmd ")
    assert out.endswith(" ok")
    assert "/Users/<redacted>" in out


def test_local_acceptance_still_masks_its_existing_input(tmp_path: Path) -> None:
    from ops import run_local_acceptance

    repo_root = tmp_path / "repo"
    out = run_local_acceptance._sanitize_text(
        f"{repo_root.as_posix()}/.venv/bin/python and /Users/local-user/private/log and {UNC}",
        repo_root=repo_root,
    )
    assert out.startswith("<repo>/.venv/bin/python and ")
    assert "local-user" not in out
    assert "/Users/<redacted>" in out
    _assert_no_account(out)


# --- ops/llm_eval.py: _sanitize_output delegates to the acceptance sanitizer -----------------


def test_llm_eval_output_masks_windows_home_spellings() -> None:
    from ops import llm_eval

    payload = llm_eval._sanitize_output(
        {"stdout": f"ran {DOUBLED}\n", "stderr": [f"at {SPACED}", "/Users/local-user/private/log"]}
    )
    text = json.dumps(payload)
    _assert_no_account(text)
    assert "local-user" not in text
    assert "/Users/<redacted>" in text


# --- core/x_editorial.py: privacy findings name "local_path" ---------------------------------


def _draft_with_post(post: str) -> dict:
    return {
        "mode": "post",
        "audience": "developers",
        "purpose": "the reader should try the flag",
        "thesis": "interrupted builds are no longer lost work",
        "title": "",
        "hook": "",
        "posts": [post],
        "sections": [],
        "media_notes": [],
        "links": [],
        "cta": "",
    }


@pytest.mark.parametrize("path", [DOUBLED, SPACED, FORWARD_SPACED, UNC])
def test_x_editorial_flags_windows_paths_as_local_path(path: str) -> None:
    from core.x_editorial import compose_xdraft, findings_of

    xdraft = compose_xdraft(
        _draft_with_post(f"Notes live at {path} today."), user_text="draft a post", source_context={}
    )
    messages = [f.message for f in findings_of(xdraft) if f.code == "privacy_leak"]
    assert any("(local_path)" in message for message in messages), messages


def test_x_editorial_still_flags_its_posix_input() -> None:
    from core.x_editorial import _privacy_hits

    assert "local_path" in _privacy_hits("Read my notes at /Users/operator-example/secrets/notes.txt.")
    assert "local_path" in _privacy_hits(f"Read my notes at {FORWARD_SPACED}.")
    assert "local_path" not in _privacy_hits("Option A: ship at 10:30, see https://example.com/changelog")


# --- core/x_voice_profile.py: add_samples refuses with "local_path" --------------------------


@pytest.mark.parametrize("path", [DOUBLED, SPACED, FORWARD_SPACED, UNC])
def test_voice_profile_refuses_windows_paths(path: str, tmp_path: Path) -> None:
    from core.x_voice_profile import _store_path, add_samples

    result = add_samples("global", [f"Notes live at {path} today."], home=tmp_path)
    assert result["status"] == "refused", result
    assert result["added"] == 0
    assert [item["reason"] for item in result["refused"]] == ["local_path"]
    store = _store_path(tmp_path)
    if store.exists():
        _assert_no_account(store.read_text(encoding="utf-8"))


def test_voice_profile_still_refuses_its_posix_input(tmp_path: Path) -> None:
    from core.x_voice_profile import add_samples

    result = add_samples(
        "global",
        ["notes live in /Users/operator-example/secrets/notes", f"notes live in {FORWARD_SPACED}"],
        home=tmp_path,
    )
    assert result["status"] == "refused", result
    assert [item["reason"] for item in result["refused"]] == ["local_path", "local_path"]
