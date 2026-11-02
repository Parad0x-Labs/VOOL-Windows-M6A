"""A signed update manifest names the same installer bytes as the release's SHA256SUMS.

release-windows.yml writes SHA256SUMS for the installer the install smoke proved, and the owner signs
those sums offline. installer.update_release builds the update manifest from whatever file sits in
--artifacts-dir. Given --sums, it refuses to sign unless every artifact's sha256 is the one the sums
name for a file of that name, so a manifest can never point updaters at bytes the smoke did not
install. Every test uses a throwaway key made in the test.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from installer.update_release import build_signed_manifest, main

INSTALLER = b"MZ installer bytes the smoke installed"


def _private_hex() -> str:
    key = Ed25519PrivateKey.generate()
    return key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()).hex()


def _release(tmp_path: Path, sums_digest: str | None = None, sums_name: str = "VOOL-Setup.exe") -> tuple[Path, Path, Path]:
    release = tmp_path / "release"
    release.mkdir()
    (release / "VOOL-Setup.exe").write_bytes(INSTALLER)
    digest = sums_digest or hashlib.sha256(INSTALLER).hexdigest()
    sums = release / "SHA256SUMS"
    sums.write_bytes(f"{'cc' * 32}  provenance.json\n{digest}  {sums_name}\n".encode("ascii"))
    template = tmp_path / "template.json"
    template.write_text(
        '{"version": "0.8.0", "artifacts": {"windows-x64": {"path": "VOOL-Setup.exe", "url": "https://example.invalid/VOOL-Setup.exe"}}}',
        encoding="utf-8",
    )
    return release, sums, template


def _build(release: Path, template: Path, sums: Path | None) -> dict:
    return build_signed_manifest(
        template_path=template,
        artifacts_dir=release,
        private_hex=_private_hex(),
        key_id="throwaway-test",
        sequence=1,
        published_at="2026-11-02T00:00:00Z",
        channel="stable",
        version="",
        minimum_compatible="",
        notes="",
        sums_path=sums,
    )


def test_an_artifact_the_sums_name_with_the_same_digest_is_signed(tmp_path):
    release, sums, template = _release(tmp_path)

    manifest = _build(release, template, sums)

    assert manifest["artifacts"]["windows-x64"]["sha256"] == hashlib.sha256(INSTALLER).hexdigest()


def test_an_artifact_whose_bytes_differ_from_the_sums_is_refused(tmp_path):
    release, sums, template = _release(tmp_path, sums_digest="dd" * 32)

    with pytest.raises(SystemExit):
        _build(release, template, sums)


def test_an_artifact_the_sums_do_not_name_is_refused(tmp_path):
    release, sums, template = _release(tmp_path, sums_name="Other-Setup.exe")

    with pytest.raises(SystemExit):
        _build(release, template, sums)


@pytest.mark.parametrize(
    "body",
    [
        b"not a sums line\n",
        ("aa" * 32 + "  VOOL-Setup.exe\r\n").encode("ascii"),
        ("aa" * 32 + "  VOOL-Setup.exe\n" + "bb" * 32 + "  VOOL-Setup.exe\n").encode("ascii"),
        ("AA" * 32 + "  VOOL-Setup.exe\n").encode("ascii"),
    ],
    ids=["no-digest", "crlf", "named-twice", "upper-hex"],
)
def test_sums_that_are_not_well_formed_are_refused(tmp_path, body):
    release, sums, template = _release(tmp_path)
    sums.write_bytes(body)

    with pytest.raises(SystemExit):
        _build(release, template, sums)


def test_the_command_line_takes_the_sums_and_refuses_a_mismatch(tmp_path, monkeypatch):
    release, sums, template = _release(tmp_path, sums_digest="dd" * 32)
    key_file = tmp_path / "secrets" / "throwaway.private.hex"
    key_file.parent.mkdir()
    key_file.write_text(_private_hex() + "\n", encoding="utf-8")
    monkeypatch.setattr("core.unattended_preflight.preflight", lambda *_args, **_kwargs: None)
    out = tmp_path / "manifest.json"

    with pytest.raises(SystemExit):
        main([
            "--template", str(template), "--artifacts-dir", str(release), "--key-file", str(key_file),
            "--key-id", "throwaway-test", "--sequence", "1", "--sums", str(sums), "--out", str(out),
        ])
    assert not out.exists()


def test_the_command_line_signs_when_the_sums_match(tmp_path, monkeypatch):
    release, sums, template = _release(tmp_path)
    key_file = tmp_path / "secrets" / "throwaway.private.hex"
    key_file.parent.mkdir()
    key_file.write_text(_private_hex() + "\n", encoding="utf-8")
    monkeypatch.setattr("core.unattended_preflight.preflight", lambda *_args, **_kwargs: None)
    out = tmp_path / "manifest.json"

    assert main([
        "--template", str(template), "--artifacts-dir", str(release), "--key-file", str(key_file),
        "--key-id", "throwaway-test", "--sequence", "1", "--sums", str(sums), "--out", str(out),
    ]) == 0
    assert out.exists()
