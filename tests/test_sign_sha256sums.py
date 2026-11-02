"""A release's SHA256SUMS is signed offline with the Ed25519 release key and checked against the pin.

The owner signs the exact SHA256SUMS bytes on their own machine; the key never reaches CI or the
cloud, and must live outside the repository. Anyone verifies the detached signature against the
public keys pinned in config/release/trusted_publisher_keys.json, never against a key the signature
file or the environment names. Every test here uses a throwaway key made in the test.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from tools import sign_sha256sums

ROOT = Path(__file__).resolve().parents[1]
SUMS = b"aa" * 32 + b"  VOOL-Setup.exe\n" + b"bb" * 32 + b"  provenance.json\n"


@pytest.fixture(autouse=True)
def _no_key_or_pin_from_the_environment(monkeypatch) -> None:
    monkeypatch.delenv("VOOL_RELEASE_SIGNING_KEY_HEX", raising=False)
    monkeypatch.delenv("VOOL_UPDATE_PUBLISHER_KEYS", raising=False)


def _throwaway_key(tmp_path: Path, key_id: str = "throwaway-test") -> tuple[Path, Path]:
    """A fresh key pair: the private hex in a key file, the public hex in a pin file, both outside the repo."""
    key = Ed25519PrivateKey.generate()
    private_hex = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()).hex()
    public_hex = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    key_file = tmp_path / "secrets" / f"{key_id}.private.hex"
    key_file.parent.mkdir()
    key_file.write_text(private_hex + "\n", encoding="utf-8")
    pins = tmp_path / "pins.json"
    pins.write_text(json.dumps({"_schema": "vool.update.trusted_publisher_keys.v1", key_id: public_hex}), encoding="utf-8")
    return key_file, pins


def _release(tmp_path: Path) -> Path:
    release = tmp_path / "release"
    release.mkdir()
    (release / "SHA256SUMS").write_bytes(SUMS)
    return release


def _sign(release: Path, key_file: Path, key_id: str = "throwaway-test") -> int:
    return sign_sha256sums.main(["sign", "--sums", str(release / "SHA256SUMS"), "--key-file", str(key_file), "--key-id", key_id])


def _verify(release: Path, pins: Path) -> int:
    return sign_sha256sums.main(["verify", "--sums", str(release / "SHA256SUMS"), "--trusted-keys", str(pins)])


def test_signed_sums_verify_against_the_pinned_key(tmp_path: Path) -> None:
    key_file, pins = _throwaway_key(tmp_path)
    release = _release(tmp_path)

    assert _sign(release, key_file) == 0

    signature = json.loads((release / "SHA256SUMS.sig").read_text(encoding="utf-8"))
    assert signature["schema"] == "vool.release_sums_signature.v1"
    assert signature["key_id"] == "throwaway-test" and signature["algorithm"] == "ed25519"
    assert len(base64.b64decode(signature["signature"])) == 64
    assert _verify(release, pins) == 0


def test_signing_leaves_the_sums_bytes_unchanged(tmp_path: Path) -> None:
    key_file, _pins = _throwaway_key(tmp_path)
    release = _release(tmp_path)

    _sign(release, key_file)

    assert (release / "SHA256SUMS").read_bytes() == SUMS


def test_changed_sums_fail_verification(tmp_path: Path) -> None:
    key_file, pins = _throwaway_key(tmp_path)
    release = _release(tmp_path)
    _sign(release, key_file)
    (release / "SHA256SUMS").write_bytes(SUMS.replace(b"aa", b"ab", 1))

    assert _verify(release, pins) != 0


def test_a_signature_by_a_key_that_is_not_pinned_fails(tmp_path: Path) -> None:
    key_file, _pins = _throwaway_key(tmp_path)
    _other_key, other_pins = _throwaway_key(tmp_path / "other")
    release = _release(tmp_path)
    _sign(release, key_file)

    assert _verify(release, other_pins) != 0


def test_a_signature_naming_another_pinned_id_fails(tmp_path: Path) -> None:
    key_file, pins = _throwaway_key(tmp_path)
    data = json.loads(pins.read_text(encoding="utf-8"))
    data["another-id"] = data["throwaway-test"]
    data["throwaway-test"] = "00" * 32
    pins.write_text(json.dumps(data), encoding="utf-8")
    release = _release(tmp_path)
    _sign(release, key_file)

    assert _verify(release, pins) != 0


def test_the_shipped_pin_does_not_trust_a_throwaway_key(tmp_path: Path) -> None:
    key_file, _pins = _throwaway_key(tmp_path, key_id="vool-release-2026")
    release = _release(tmp_path)
    _sign(release, key_file, key_id="vool-release-2026")

    assert sign_sha256sums.main(["verify", "--sums", str(release / "SHA256SUMS")]) != 0


def test_a_key_file_inside_the_repository_is_refused(tmp_path: Path) -> None:
    release = _release(tmp_path)

    with pytest.raises(SystemExit):
        _sign(release, ROOT / "config" / "release" / "trusted_publisher_keys.json")
    assert not (release / "SHA256SUMS.sig").exists()


def test_a_trust_override_in_the_environment_is_ignored(tmp_path: Path, monkeypatch) -> None:
    key_file, pins = _throwaway_key(tmp_path)
    public_hex = json.loads(pins.read_text(encoding="utf-8"))["throwaway-test"]
    monkeypatch.setenv("VOOL_UPDATE_PUBLISHER_KEYS", f"throwaway-test:{public_hex}")
    release = _release(tmp_path)
    _sign(release, key_file)

    assert sign_sha256sums.main(["verify", "--sums", str(release / "SHA256SUMS")]) != 0
