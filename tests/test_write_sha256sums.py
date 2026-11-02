"""SHA256SUMS for a release is the same bytes on every OS, so the owner can sign it offline with the
Ed25519 release key (docs/RELEASE_KEY_CEREMONY.md) and anyone can check it with `sha256sum -c`
(the exact bytes are pinned below, so the test needs no sha256sum binary, which Windows lacks).

One line per file, "<lowercase sha256>  <name>\\n", sorted by name, ASCII with LF line endings and
no BOM: PowerShell's own writers would add a BOM or CRLF on Windows and change the signed bytes.
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

from tools import write_sha256sums

ROOT = Path(__file__).resolve().parents[1]


def _release(tmp_path: Path) -> Path:
    (tmp_path / "VOOL-Setup.exe").write_bytes(b"MZ installer bytes")
    (tmp_path / "provenance.json").write_bytes(b'{"commit": "abc"}\r\n')
    return tmp_path


def test_sums_are_sorted_lf_lines_that_sha256sum_reads(tmp_path: Path) -> None:
    release = _release(tmp_path)

    assert write_sha256sums.main(["--dir", str(release), "provenance.json", "VOOL-Setup.exe"]) == 0

    raw = (release / "SHA256SUMS").read_bytes()
    assert raw == (
        f"{hashlib.sha256(b'MZ installer bytes').hexdigest()}  VOOL-Setup.exe\n"
        f"{hashlib.sha256(b'{\"commit\": \"abc\"}' + b'\r\n').hexdigest()}  provenance.json\n"
    ).encode("ascii")


def test_check_passes_for_the_files_as_written(tmp_path: Path) -> None:
    release = _release(tmp_path)
    write_sha256sums.main(["--dir", str(release), "VOOL-Setup.exe", "provenance.json"])

    assert write_sha256sums.main(["--check", "--dir", str(release)]) == 0


def test_check_fails_when_a_listed_file_changed_after_the_sums(tmp_path: Path) -> None:
    release = _release(tmp_path)
    write_sha256sums.main(["--dir", str(release), "VOOL-Setup.exe", "provenance.json"])
    (release / "VOOL-Setup.exe").write_bytes(b"MZ other bytes")

    assert write_sha256sums.main(["--check", "--dir", str(release)]) != 0


@pytest.mark.parametrize("name", ["../outside.exe", "sub/VOOL-Setup.exe", "SHA256SUMS", ""])
def test_only_plain_file_names_in_the_release_folder_are_summed(tmp_path: Path, name: str) -> None:
    release = _release(tmp_path)

    with pytest.raises(SystemExit):
        write_sha256sums.main(["--dir", str(release), name])
    assert not (release / "SHA256SUMS").exists()


def test_it_runs_as_a_script_from_the_repo_root(tmp_path: Path) -> None:
    release = _release(tmp_path)

    result = subprocess.run(
        [sys.executable, "tools/write_sha256sums.py", "--dir", str(release), "VOOL-Setup.exe"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
