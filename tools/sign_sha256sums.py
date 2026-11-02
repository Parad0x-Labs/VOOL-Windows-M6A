"""Sign a release's SHA256SUMS with the Ed25519 release key, offline, or verify that signature.

The owner runs `sign` on their own machine after downloading the workflow artifact. It signs the
exact SHA256SUMS bytes (tools/write_sha256sums.py) and writes a detached SHA256SUMS.sig. The private
key comes from a file outside this repository or VOOL_RELEASE_SIGNING_KEY_HEX, as for the update
manifest (installer/update_release.py, docs/RELEASE_KEY_CEREMONY.md); it never reaches CI.

`verify` checks the signature against the publisher keys pinned in
config/release/trusted_publisher_keys.json, or an explicit --trusted-keys file. It never trusts a
key named by the signature file or by the environment.

    python tools/sign_sha256sums.py sign --sums <release>/SHA256SUMS --key-file <outside-repo> --key-id <id>
    python tools/sign_sha256sums.py verify --sums <release>/SHA256SUMS
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.updater.trust import TrustedPublishers
from installer.update_release import load_private_key, sign

SCHEMA = "vool.release_sums_signature.v1"


def _sign(sums: Path, key_file: Path | None, key_id: str) -> int:
    payload = sums.read_bytes()
    private_hex = load_private_key(key_file, dict(os.environ))
    document = {
        "schema": SCHEMA,
        "key_id": key_id,
        "algorithm": "ed25519",
        "signed_file": sums.name,
        "sums_sha256": hashlib.sha256(payload).hexdigest(),
        "signature": sign(private_hex, payload),
    }
    out = sums.with_name(sums.name + ".sig")
    out.write_bytes((json.dumps(document, indent=2) + "\n").encode("ascii"))
    print(f"wrote {out} (key {key_id})")
    return 0


def _verify(sums: Path, signature_path: Path, trusted_keys: Path | None) -> int:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    payload = sums.read_bytes()
    try:
        document = json.loads(signature_path.read_text(encoding="utf-8"))
        key_id = str(document["key_id"])
        signature = base64.b64decode(str(document["signature"]), validate=True)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"FAIL: unreadable signature {signature_path}: {exc}")
        return 1
    if document.get("schema") != SCHEMA or document.get("algorithm") != "ed25519":
        print("FAIL: not an Ed25519 SHA256SUMS signature")
        return 1
    # env={}: a key pinned through the environment is not a release pin.
    public_hex = TrustedPublishers.load(trusted_keys, env={}).public_key_hex(key_id)
    if not public_hex:
        print(f"FAIL: key {key_id!r} is not pinned")
        return 1
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex)).verify(signature, payload)
    except (InvalidSignature, ValueError):
        print(f"FAIL: the signature does not match {sums.name} under key {key_id!r}")
        return 1
    print(f"OK: {sums.name} signed by pinned key {key_id!r}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sign_sha256sums")
    commands = parser.add_subparsers(dest="command", required=True)
    signer = commands.add_parser("sign")
    signer.add_argument("--sums", required=True)
    signer.add_argument("--key-file", default="", help="private key file, outside this repository")
    signer.add_argument("--key-id", required=True)
    verifier = commands.add_parser("verify")
    verifier.add_argument("--sums", required=True)
    verifier.add_argument("--signature", default="", help="default: <sums>.sig")
    verifier.add_argument("--trusted-keys", default="", help="default: config/release/trusted_publisher_keys.json")
    args = parser.parse_args(argv)
    sums = Path(args.sums)
    if args.command == "sign":
        return _sign(sums, Path(args.key_file) if args.key_file else None, args.key_id)
    signature = Path(args.signature) if args.signature else sums.with_name(sums.name + ".sig")
    return _verify(sums, signature, Path(args.trusted_keys) if args.trusted_keys else None)


if __name__ == "__main__":
    raise SystemExit(main())
