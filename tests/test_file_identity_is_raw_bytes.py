"""A file's hash is sha256 of its bytes on disk, and no writer changes bytes it was not asked to.

machine.read_file hashed text read with universal newlines and U+FFFD for bad bytes, while the
workspace writers compare against the file's own bytes, so a CRLF, lone-CR or mixed file read
through machine.read_file could never be written back with the hash that read returned. The same
folding let an unreviewed patch, and its rollback, turn a CRLF file into LF, and let
replace_in_file write U+FFFD over bytes that were not UTF-8. machine.write_file wrote through text
mode, so on Windows each "\\n" it was handed landed as "\\r\\n".
"""
from __future__ import annotations

import hashlib
import pathlib
import subprocess
from pathlib import Path

import pytest

from core.repoops.wsfs import WorkspaceFS
from core.runtime_execution_tools import execute_runtime_tool

FILES = {
    "lf": b"alpha\nbeta\n",
    "crlf": b"alpha\r\nbeta\r\n",
    "lone-cr": b"alpha\rbeta\r",
    "mixed": b"alpha\r\nbeta\ngamma\r\n",
    "bom": b"\xef\xbb\xbfalpha\nbeta\n",
}
BAD_BYTES = b"it\x92s here\n"


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home whose ``Documents/work`` folder is also the workspace, so both tool surfaces see one file."""
    (tmp_path / "Documents" / "work").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


def _machine_read(name: str) -> dict:
    result = execute_runtime_tool("machine.read_file", {"path": f"~/Documents/work/{name}"}, source_context={})
    assert result is not None and result.ok, result and result.response_text
    return result.details


def _workspace(tool: str, home: Path, **arguments):
    result = execute_runtime_tool(tool, arguments, source_context={"workspace": str(home / "Documents" / "work")})
    assert result is not None
    return result


@pytest.mark.parametrize("case", sorted(FILES))
def test_machine_read_file_hashes_the_bytes_on_disk(home: Path, case: str) -> None:
    (home / "Documents" / "work" / "f.txt").write_bytes(FILES[case])

    assert _machine_read("f.txt")["hash"] == _sha(FILES[case])


def test_machine_read_file_hashes_bad_bytes_as_they_are_and_says_the_text_is_lossy(home: Path) -> None:
    (home / "Documents" / "work" / "f.txt").write_bytes(BAD_BYTES)

    details = _machine_read("f.txt")

    assert details["hash"] == _sha(BAD_BYTES)
    assert details.get("decode_lossy") is True


def test_workspace_read_file_hashes_bad_bytes_as_they_are(home: Path) -> None:
    (home / "Documents" / "work" / "f.txt").write_bytes(BAD_BYTES)

    result = _workspace("workspace.read_file", home, path="f.txt")

    assert result.ok and result.details["hash"] == _sha(BAD_BYTES)


@pytest.mark.parametrize("case", ["crlf", "lone-cr", "mixed"])
def test_a_machine_read_hash_lets_workspace_write_file_replace_the_file(home: Path, case: str) -> None:
    target = home / "Documents" / "work" / "f.txt"
    target.write_bytes(FILES[case])
    digest = _machine_read("f.txt")["hash"]

    result = _workspace("workspace.write_file", home, path="f.txt", content="new\r\nbody\r\n", expected_hash=digest)

    assert result.ok, result.response_text
    assert target.read_bytes() == b"new\r\nbody\r\n"


@pytest.mark.parametrize("case", ["crlf", "lone-cr", "mixed"])
def test_a_machine_read_hash_lets_replace_in_file_edit_the_file(home: Path, case: str) -> None:
    target = home / "Documents" / "work" / "f.txt"
    target.write_bytes(FILES[case])
    digest = _machine_read("f.txt")["hash"]

    result = _workspace(
        "workspace.replace_in_file", home, path="f.txt", old_text="beta", new_text="BETA", expected_hash=digest
    )

    assert result.ok, result.response_text
    assert target.read_bytes() == FILES[case].replace(b"beta", b"BETA")


def test_a_workspace_read_hash_of_bad_bytes_lets_write_file_replace_the_file(home: Path) -> None:
    target = home / "Documents" / "work" / "f.txt"
    target.write_bytes(BAD_BYTES)
    digest = _workspace("workspace.read_file", home, path="f.txt").details["hash"]

    result = _workspace("workspace.write_file", home, path="f.txt", content="fixed\n", expected_hash=digest)

    assert result.ok, result.response_text
    assert target.read_bytes() == b"fixed\n"


def test_replace_in_file_refuses_a_file_that_is_not_utf8_and_leaves_its_bytes(home: Path) -> None:
    target = home / "Documents" / "work" / "f.txt"
    target.write_bytes(BAD_BYTES)

    result = _workspace("workspace.replace_in_file", home, path="f.txt", old_text="here", new_text="there")

    assert not result.ok
    assert target.read_bytes() == BAD_BYTES


def _git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True)


@pytest.mark.parametrize("patch_newline", ["\n", "\r\n"])
def test_an_unreviewed_patch_never_folds_a_crlf_file_to_lf(home: Path, patch_newline: str) -> None:
    root = home / "Documents" / "work"
    _git_repo(root)
    target = root / "f.txt"
    original = b"one\r\ntwo\r\nthree\r\n"
    target.write_bytes(original)
    patch = patch_newline.join(["--- a/f.txt", "+++ b/f.txt", "@@ -1,3 +1,3 @@", " one", "-two", "+TWO", " three", ""])

    result = _workspace("workspace.apply_unified_diff", home, patch=patch)

    if result.ok:
        assert target.read_bytes() == b"one\r\nTWO\r\nthree\r\n"
    else:
        assert target.read_bytes() == original


def test_rolling_back_a_patch_on_a_crlf_file_restores_its_exact_bytes(home: Path) -> None:
    root = home / "Documents" / "work"
    _git_repo(root)
    target = root / "f.txt"
    original = b"one\r\ntwo\r\nthree\r\n"
    target.write_bytes(original)
    patch = "\n".join(["--- a/f.txt", "+++ b/f.txt", "@@ -1,3 +1,3 @@", " one", "-two", "+TWO", " three", ""])

    applied = _workspace("workspace.apply_unified_diff", home, patch=patch)
    if applied.ok:
        _workspace("workspace.rollback_last_change", home)

    assert target.read_bytes() == original


def _text_mode_as_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """``Path.write_text`` with no ``newline`` writes each "\\n" as os.linesep, which is "\\r\\n" on Windows."""
    real = pathlib.Path.write_text

    def write_text(self, data, encoding=None, errors=None, newline=None):
        if newline is None:
            data, newline = data.replace("\n", "\r\n"), ""
        return real(self, data, encoding=encoding, errors=errors, newline=newline)

    monkeypatch.setattr(pathlib.Path, "write_text", write_text)


@pytest.mark.parametrize("content", ["a\nb\n", "a\r\nb\r\n"])
def test_machine_write_file_writes_exactly_the_bytes_it_was_given(home: Path, monkeypatch, content: str) -> None:
    _text_mode_as_on_windows(monkeypatch)
    target = home / "Documents" / "work" / "out.txt"

    result = execute_runtime_tool(
        "machine.write_file", {"path": "~/Documents/work/out.txt", "content": content}, source_context={}
    )

    assert result is not None and result.ok, result and result.response_text
    assert target.read_bytes() == content.encode("utf-8")


def _workspace_fs(root: Path) -> WorkspaceFS:
    return WorkspaceFS(type("Workspace", (), {"root": str(root), "key": "test"})())


class _Broker:
    def execute(self, fork, request, dispatch):
        return dispatch()


@pytest.mark.parametrize("content", ["a\nb\n", "a\r\nb\r\n"])
def test_workspace_fs_keeps_the_bytes_it_reads_and_writes(tmp_path: Path, monkeypatch, content: str) -> None:
    _text_mode_as_on_windows(monkeypatch)
    fs = _workspace_fs(tmp_path)

    outcome = fs.write(None, _Broker(), "f.txt", content)

    on_disk = (tmp_path / "f.txt").read_bytes()
    assert on_disk == content.encode("utf-8")
    assert outcome.evidence["bytes"] == len(on_disk)
    assert fs.read("f.txt") == content
