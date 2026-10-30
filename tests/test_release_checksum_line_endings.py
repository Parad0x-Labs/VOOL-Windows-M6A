"""Release checksum files are published as exact LF bytes on every platform.

`sha256sum -c` (what a Linux or macOS user runs against a downloaded sidecar) reads a
trailing CR as part of the FILENAME, so a CRLF checksum file names an artifact that is
not there and verification fails. On Windows a text-mode write emits CRLF, so the
checksum writer must publish bytes, not platform text.
"""

from __future__ import annotations

from pathlib import Path

from installer import release_sign


def _release_dir(tmp_path: Path) -> Path:
    d = tmp_path / "release"
    d.mkdir()
    (d / "vool-0.7.0-source.tar.gz").write_bytes(b"source tarball bytes\nwith lines\n")
    (d / "install_vool.sh").write_bytes(b"#!/bin/sh\necho install\n")
    return d


def test_checksum_sidecars_are_exact_lf_bytes_on_every_platform(tmp_path: Path) -> None:
    release_dir = _release_dir(tmp_path)

    names = release_sign.checksums(release_dir)

    for name in names:  # the website serves one sidecar per download
        digest = release_sign.sha256_of(release_dir / name)
        expected_line = f"{digest}  {name}\n".encode("ascii")
        sidecar = release_dir / (name + release_sign.SIDECAR_SUFFIX)
        assert sidecar.read_bytes() == expected_line, "a sidecar must be LF bytes or sha256sum -c reads the CR as part of the name"


def test_sha256sums_is_exact_lf_bytes_on_every_platform(tmp_path: Path) -> None:
    release_dir = _release_dir(tmp_path)

    names = release_sign.checksums(release_dir)

    expected = b"".join(f"{release_sign.sha256_of(release_dir / name)}  {name}\n".encode("ascii") for name in names)
    assert (release_dir / release_sign.SUMS).read_bytes() == expected, "SHA256SUMS must be LF bytes on every platform"
