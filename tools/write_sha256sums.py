"""Write or check SHA256SUMS for a release folder, as the same bytes on every OS.

One line per file, "<lowercase sha256>  <name>\\n", sorted by name, ASCII with LF line endings and no
BOM, so `sha256sum -c` reads it and the owner's offline Ed25519 signature over it
(docs/RELEASE_KEY_CEREMONY.md) covers identical bytes whichever OS wrote it. The signing key never
reaches CI; signing is a human step.

    python tools/write_sha256sums.py --dir <release> VOOL-Setup.exe provenance.json
    python tools/write_sha256sums.py --check --dir <release>
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

SUMS = "SHA256SUMS"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _plain_name(parser: argparse.ArgumentParser, name: str) -> str:
    if not name or name in {".", "..", SUMS} or Path(name).name != name or "\\" in name:
        parser.error(f"not a plain file name in the release folder: {name!r}")
    return name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="write_sha256sums")
    parser.add_argument("--dir", required=True, help="the release folder")
    parser.add_argument("--check", action="store_true", help="verify the existing SHA256SUMS instead")
    parser.add_argument("files", nargs="*", help="file names inside --dir to list")
    args = parser.parse_args(argv)
    folder = Path(args.dir)
    if args.check:
        lines = (folder / SUMS).read_bytes().decode("ascii").split("\n")
        if lines[-1] != "" or len(lines) < 2:
            print(f"{SUMS} is empty or does not end with a newline")
            return 1
        for line in lines[:-1]:
            expected, _, name = line.partition("  ")
            name = _plain_name(parser, name)
            actual = _sha256(folder / name)
            if actual != expected:
                print(f"{name}: expected {expected}, found {actual}")
                return 1
            print(f"{name}: OK")
        return 0
    if not args.files:
        parser.error("name at least one file")
    names = sorted({_plain_name(parser, name) for name in args.files})
    body = "".join(f"{_sha256(folder / name)}  {name}\n" for name in names)
    (folder / SUMS).write_bytes(body.encode("ascii"))
    print(body, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
