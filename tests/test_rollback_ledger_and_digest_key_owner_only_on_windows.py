"""On Windows the mutation (rollback) ledger and the receipts digest key go through core/owner_only.

The rollback ledger keeps a file's before_text verbatim (a rolled-back .env leaves real key material
on disk by design) and the digest key keys every ledger digest, so both must be owner-only. On
Windows ``os.chmod`` and an ``os.open`` mode only touch the read-only bit, so the folder is created
owner-only and each file is put through ``require_owner_only`` before its bytes land; when that is
unavailable the write raises instead. These tests drive the Windows branch on any host.
"""
from __future__ import annotations

import pytest

from core.evidence_kernel import receipts
from core.execution import artifacts
from core.owner_only import OwnerOnlyUnavailable


def _refuse(path, **_kwargs):
    raise OwnerOnlyUnavailable(path, "test_refusal")


@pytest.fixture
def ledger_home(tmp_path, monkeypatch):
    monkeypatch.setenv("VOOL_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(artifacts, "_WINDOWS_OWNER_ONLY", True)
    return tmp_path


def test_the_rollback_ledger_is_protected_before_its_bytes_land(ledger_home, monkeypatch):
    calls = []

    def _ensure(path, *, fresh=False):
        calls.append(("dir", path))
        path.mkdir(parents=True, exist_ok=True)
        return path

    monkeypatch.setattr(artifacts, "ensure_private_dir", _ensure)
    monkeypatch.setattr(artifacts, "require_owner_only", lambda path, *, directory=False: calls.append(("file", path.read_text(encoding="utf-8"))))
    artifacts._store_mutation_records("sess-acl", [{"path": "x.env", "before_text": "API_KEY=sk-live\n"}])
    path = artifacts._mutation_log_path("sess-acl")
    assert calls == [("dir", path.parent), ("file", "")]
    assert [p.name for p in path.parent.iterdir()] == [path.name]
    assert artifacts._load_mutation_records("sess-acl")[0]["before_text"] == "API_KEY=sk-live\n"


def test_no_rollback_bytes_are_written_when_the_ledger_folder_cannot_be_owner_only(ledger_home, monkeypatch):
    monkeypatch.setattr(artifacts, "ensure_private_dir", _refuse)
    with pytest.raises(OwnerOnlyUnavailable):
        artifacts._store_mutation_records("sess-acl", [{"path": "x.env", "before_text": "API_KEY=sk-live\n"}])
    assert not artifacts._mutation_log_path("sess-acl").exists()


def test_an_existing_ledger_survives_a_refusal_unchanged(ledger_home, monkeypatch):
    def _ensure(path, *, fresh=False):
        path.mkdir(parents=True, exist_ok=True)
        return path

    monkeypatch.setattr(artifacts, "ensure_private_dir", _ensure)
    monkeypatch.setattr(artifacts, "require_owner_only", lambda path, *, directory=False: None)
    first = [{"path": "x.env", "before_text": "API_KEY=sk-live\n"}]
    artifacts._store_mutation_records("sess-acl", first)
    path = artifacts._mutation_log_path("sess-acl")
    before = path.read_bytes()

    monkeypatch.setattr(artifacts, "require_owner_only", _refuse)
    with pytest.raises(OwnerOnlyUnavailable):
        artifacts._store_mutation_records("sess-acl", [*first, {"path": "y.env", "before_text": "B=2\n"}])
    assert path.read_bytes() == before
    assert artifacts._load_mutation_records("sess-acl") == first
    assert [p.name for p in path.parent.iterdir()] == [path.name]


def test_the_digest_key_is_protected_before_it_lands(tmp_path, monkeypatch):
    monkeypatch.setattr(receipts, "_WINDOWS_OWNER_ONLY", True)
    monkeypatch.setattr(receipts, "_ledger_dir", lambda: tmp_path / "receipts_v2")
    from core import owner_only

    seen = []
    monkeypatch.setattr(owner_only, "require_owner_only", lambda path, *, directory=False: seen.append(path.read_bytes()))
    key = receipts._digest_key()
    assert seen == [b""]
    assert len(key) == 32


def test_no_digest_key_goes_live_when_owner_only_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(receipts, "_WINDOWS_OWNER_ONLY", True)
    monkeypatch.setattr(receipts, "_ledger_dir", lambda: tmp_path / "receipts_v2")
    from core import owner_only

    monkeypatch.setattr(owner_only, "require_owner_only", _refuse)
    with pytest.raises(OwnerOnlyUnavailable):
        receipts._digest_key()
    assert not (tmp_path / "receipts_v2" / ".digest_key").exists()
