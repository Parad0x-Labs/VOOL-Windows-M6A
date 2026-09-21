"""Per-launch Windows bundle proof for privileged loopback HTTP requests.

The supervisor hands the API and native window a secret through anonymous stdin
pipes. The secret never belongs in a URL, request header, page script, environment
variable, log, or persisted state. A proof binds one request's method, path and
body to a short-lived nonce; accepting it consumes that nonce.

This is a transport gate, not a native user-approval UI or a replacement for
the command policy. Other privileged routes remain outside this first slice.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
from collections.abc import Mapping

SECRET_BYTES = 32
_MAX_AGE_SECONDS = 60
_NONCE_RETENTION_SECONDS = 120
_MAX_NONCES = 4096
_NONCE = re.compile(r"^[A-Za-z0-9_-]{20,80}$")
_PROTECTED_PATHS = frozenset({"/api/mode", "/api/commands/dispatch"})
_VERIFIER: NativeControlVerifier | None = None


class NativeControlUnavailable(RuntimeError):
    """A Windows bundle child did not receive its private bootstrap pipe."""


def windows_bundle() -> bool:
    return os.environ.get("VOOL_WINDOWS_BUNDLE") == "1"


def protected_path(method: str, path: str) -> bool:
    route = str(path or "").split("?", 1)[0].rstrip("/") or "/"
    return str(method or "").upper() != "GET" and route in _PROTECTED_PATHS


def read_bootstrap_secret() -> bytes:
    """Read exactly one secret from inherited stdin, then remove the non-secret marker."""
    if os.environ.pop("VOOL_NATIVE_CONTROL_STDIN", "") != "1":
        raise NativeControlUnavailable("native control pipe was not supplied")
    stream = getattr(sys.stdin, "buffer", None)
    if stream is None:
        raise NativeControlUnavailable("native control pipe is unavailable")
    chunks = bytearray()
    while len(chunks) < SECRET_BYTES:
        part = stream.read(SECRET_BYTES - len(chunks))
        if not part:
            raise NativeControlUnavailable("native control pipe closed before bootstrap")
        chunks.extend(part)
    return bytes(chunks)


def _message(method: str, path: str, body: bytes, nonce: str, issued_at: str) -> bytes:
    fields = [str(method).upper(), str(path), nonce, issued_at, hashlib.sha256(body).hexdigest()]
    return json.dumps(fields, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def sign_headers(
    secret: bytes, method: str, path: str, body: bytes, *,
    nonce: str | None = None, issued_at: int | None = None,
) -> dict[str, str]:
    if len(secret) != SECRET_BYTES:
        raise ValueError("native control secret must be 32 bytes")
    clean_nonce = nonce or secrets.token_urlsafe(24)
    if not _NONCE.fullmatch(clean_nonce):
        raise ValueError("invalid native control nonce")
    stamp = str(int(time.time()) if issued_at is None else int(issued_at))
    proof = hmac.new(secret, _message(method, path, body, clean_nonce, stamp), hashlib.sha256).hexdigest()
    return {
        "X-VOOL-Native-Nonce": clean_nonce,
        "X-VOOL-Native-Time": stamp,
        "X-VOOL-Native-Proof": proof,
    }


def _header(headers: Mapping[str, str], name: str) -> str:
    wanted = name.lower()
    return next((str(value) for key, value in headers.items() if str(key).lower() == wanted), "")


class NativeControlVerifier:
    def __init__(self, secret: bytes) -> None:
        if len(secret) != SECRET_BYTES:
            raise ValueError("native control secret must be 32 bytes")
        self._secret = bytes(secret)
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def verify(self, method: str, path: str, body: bytes, headers: Mapping[str, str]) -> bool:
        nonce = _header(headers, "X-VOOL-Native-Nonce")
        stamp = _header(headers, "X-VOOL-Native-Time")
        supplied = _header(headers, "X-VOOL-Native-Proof")
        if not _NONCE.fullmatch(nonce) or len(supplied) != 64:
            return False
        try:
            issued_at = int(stamp)
        except ValueError:
            return False
        now = time.time()
        if abs(now - issued_at) > _MAX_AGE_SECONDS:
            return False
        expected = hmac.new(
            self._secret, _message(method, path, body, nonce, stamp), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, supplied):
            return False
        with self._lock:
            self._seen = {
                key: accepted_at for key, accepted_at in self._seen.items()
                if now - accepted_at <= _NONCE_RETENTION_SECONDS
            }
            if nonce in self._seen or len(self._seen) >= _MAX_NONCES:
                return False
            self._seen[nonce] = now
        return True


def install_secret(secret: bytes | None) -> None:
    """Install only process-local verifier state; None invalidates prior proofs."""
    global _VERIFIER
    _VERIFIER = NativeControlVerifier(secret) if secret is not None else None


def authorize_http_request(method: str, path: str, body: bytes, headers: Mapping[str, str]) -> bool:
    if not windows_bundle() or not protected_path(method, path):
        return True
    verifier = _VERIFIER
    return verifier is not None and verifier.verify(method, path, body, headers)
