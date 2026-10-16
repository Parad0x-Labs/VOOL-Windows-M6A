"""A resolved machine file reaches the reader; refusal still precedes content inspection."""
from __future__ import annotations

from pathlib import Path

import pytest

from core import runtime_execution_tools as tools
from core.execution.artifacts import content_sha256


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "machine-home"
    for name in ("Desktop", "Downloads", "Documents"):
        (root / name).mkdir(parents=True)
    monkeypatch.setattr(tools, "_machine_home", lambda: root)
    return root


def _read(path: str, **arguments):
    return tools.execute_runtime_tool("machine.read_file", {"path": path, **arguments}, source_context={})


@pytest.mark.parametrize("folder", ["Desktop", "Downloads", "Documents"])
@pytest.mark.parametrize("absolute", [False, True])
def test_a_resolved_safe_file_reaches_the_reader(home: Path, folder: str, absolute: bool) -> None:
    target = home / folder / "notes.txt"
    text = "first line\nsecond line\n"
    target.write_text(text, encoding="utf-8")
    result = _read(str(target) if absolute else f"~/{folder}/notes.txt")
    assert result.ok and result.status == "executed"
    assert result.details["hash"] == content_sha256(text)
    assert result.details["lines"] == [
        {"line_number": 1, "text": "first line"}, {"line_number": 2, "text": "second line"},
    ]
    assert result.details["observation"]["ok"] is True
    assert result.details["observation"]["intent"] == "machine.read_file"


def test_a_resolved_read_keeps_its_slice_hash_and_fragment_warning(home: Path) -> None:
    text = "one\ntwo\nthree\nfour\n"
    (home / "Desktop" / "lines.txt").write_text(text, encoding="utf-8")
    result = _read("~/Desktop/lines.txt", start_line=2, max_lines=1, verbatim=True)
    assert result.ok and result.status == "truncated"
    assert result.details["lines"] == [{"line_number": 2, "text": "two"}]
    assert result.details["total_lines"] == 4 and result.details["truncated"] is True
    assert result.details["hash"] == content_sha256(text)
    assert result.response_text.startswith("two\n") and "This is a fragment" in result.response_text


def test_an_empty_resolved_slice_is_not_reported_as_a_path_refusal(home: Path) -> None:
    (home / "Documents" / "short.txt").write_text("one\n", encoding="utf-8")
    result = _read("~/Documents/short.txt", start_line=5)
    assert result.ok and result.status == "empty_slice"
    assert result.details["line_count"] == 0 and result.details["lines"] == []


@pytest.mark.parametrize("path", ["~/Desktop/missing.txt", "~/Documents"])
def test_a_resolved_non_file_keeps_the_existing_not_found_result(home: Path, path: str) -> None:
    result = _read(path)
    assert not result.ok and result.status == "not_found"
    assert result.details["observation"]["status"] == "not_found"


def test_a_resolved_binary_is_still_refused(home: Path) -> None:
    (home / "Downloads" / "binary.bin").write_bytes(b"fixture\x00payload")
    result = _read("~/Downloads/binary.bin")
    assert not result.ok and result.status == "binary_file"
    assert "lines" not in result.details


def _no_content_inspection(_path: Path) -> bool:
    raise AssertionError("a refused path must not reach content inspection")


@pytest.mark.parametrize("path", ["../private.txt", "~/Desktop/../../private.txt", "~/Desktop-other/private.txt"])
def test_an_outside_or_traversal_path_still_stops_before_content_inspection(
    home: Path, monkeypatch: pytest.MonkeyPatch, path: str,
) -> None:
    (home.parent / "private.txt").write_text("outside fixture content", encoding="utf-8")
    (home / "Desktop-other").mkdir()
    (home / "Desktop-other" / "private.txt").write_text("outside fixture content", encoding="utf-8")
    monkeypatch.setattr(tools, "_is_probably_text", _no_content_inspection)
    result = _read(path)
    assert not result.ok and result.status == "not_allowed"
    assert result.details["observation"]["requested_path"]
    assert "outside fixture content" not in result.response_text


@pytest.mark.parametrize("hardlink", [False, True])
def test_runtime_state_and_its_hardlink_still_stop_before_content_inspection(
    home: Path, monkeypatch: pytest.MonkeyPatch, hardlink: bool,
) -> None:
    from core import runtime_state_protection

    state = home / "Documents" / "runtime-state"
    state.mkdir()
    target = state / "record.txt"
    target.write_text("protected fixture content", encoding="utf-8")
    monkeypatch.setattr(runtime_state_protection, "protected_state_roots", lambda: (state,))
    if hardlink:
        alias = home / "Desktop" / "alias.txt"
        alias.hardlink_to(target)
        target = alias
    monkeypatch.setattr(tools, "_is_probably_text", _no_content_inspection)
    result = _read(str(target))
    assert not result.ok and result.status == "not_allowed"
    assert result.details["observation"]["reason"] == "protected_runtime_state"
    assert "protected fixture content" not in result.response_text


def test_disabled_reads_stop_before_path_resolution(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original_get = tools.policy_engine.get
    monkeypatch.setattr(
        tools.policy_engine, "get",
        lambda key, default=None: False if key == "filesystem.allow_read_workspace" else original_get(key, default),
    )
    monkeypatch.setattr(tools, "_resolve_machine_directory", _no_content_inspection)
    result = _read("~/Documents/notes.txt")
    assert not result.ok and result.status == "disabled"
    assert result.details["observation"]["status"] == "disabled"
