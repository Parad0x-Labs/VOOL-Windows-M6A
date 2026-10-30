"""`changed_test_files` ids in the trusted-baseline artifact are forward-slash.

The artifact (`vool.trusted-baseline-comparison.v1.json`) is compared across revisions
and platforms; a Windows-produced `tests\\test_demo.py` key never equals a POSIX-produced
`tests/test_demo.py`, so the same bytes read as changed on one platform and unchanged on
another. Pinned red-first on Windows.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from ops.trusted_controller import _target_file_digests


def test_target_file_digest_keys_are_posix_repo_ids(tmp_path: Path) -> None:
    demo = tmp_path / "tests" / "test_demo.py"
    demo.parent.mkdir(parents=True)
    demo.write_text("def test_demo():\n    assert True\n", encoding="utf-8")

    digests = _target_file_digests(tmp_path, "tests/test_demo.py")

    assert list(digests) == ["tests/test_demo.py"]
    assert digests["tests/test_demo.py"] == hashlib.sha256(demo.read_bytes()).hexdigest()
