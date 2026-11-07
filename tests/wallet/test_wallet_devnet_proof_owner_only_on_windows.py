"""On Windows the devnet proof's home and resume file are owner-only through core/owner_only.

``os.chmod`` only flips the read-only bit there, so the isolated home is created owner-only (files
written inside inherit that ACE), the resume file holding the payer PIN and key passphrase is put
through ``require_owner_only`` before any byte lands, and selfcheck judges privacy by the ACL rather
than by POSIX mode strings. These tests drive the Windows branch on any host by setting ``_WINDOWS``
and standing in for the core/owner_only calls.
"""
from __future__ import annotations

import pytest

from core import owner_only
from core.owner_only import OwnerOnlyReport, OwnerOnlyUnavailable
from ops import wallet_devnet_external_proof as proof


def test_the_resume_file_is_protected_before_any_secret_lands(tmp_path, monkeypatch):
    monkeypatch.setattr(proof, "_WINDOWS", True)
    seen = []
    monkeypatch.setattr(owner_only, "require_owner_only", lambda path, *, directory=False: seen.append(path.read_text(encoding="utf-8")))
    target = tmp_path / "resume.json"
    proof._write_private(target, {"payer_pin": "246810"})
    assert seen == [""]
    assert "246810" in target.read_text(encoding="utf-8")


def test_no_resume_file_is_left_when_owner_only_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(proof, "_WINDOWS", True)

    def _refuse(path, *, directory=False):
        raise OwnerOnlyUnavailable(path, "test_refusal")

    monkeypatch.setattr(owner_only, "require_owner_only", _refuse)
    target = tmp_path / "resume.json"
    with pytest.raises(OwnerOnlyUnavailable):
        proof._write_private(target, {"payer_pin": "246810"})
    assert not target.exists()


def test_the_isolated_home_is_created_owner_only(tmp_path, monkeypatch):
    monkeypatch.setattr(proof, "_WINDOWS", True)
    made = []

    def _ensure(path, *, fresh=False):
        made.append(path)
        raise OwnerOnlyUnavailable(path, "test_refusal")

    monkeypatch.setattr(owner_only, "ensure_private_dir", _ensure)
    home = tmp_path / "isolated-home"
    with pytest.raises(OwnerOnlyUnavailable):
        proof._bootstrap(home, None, rpc_url="http://127.0.0.1:1")
    assert made == [home]
    assert not home.exists()


def test_privacy_is_judged_by_the_acl_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(proof, "_WINDOWS", True)
    target = tmp_path / "resume.json"
    target.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(owner_only, "verify_owner_only", lambda path: OwnerOnlyReport(True, ""))
    assert proof._is_private(target, "0o600")
    monkeypatch.setattr(owner_only, "verify_owner_only", lambda path: OwnerOnlyReport(False, "extra_ace"))
    assert not proof._is_private(target, "0o600")
