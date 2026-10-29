"""What a document refused for want of decoder confinement tells the operator to do, per host.

PDF, .doc, RAR, 7z, audio and video are read by external decoders, and a host that cannot confine
them refuses before a byte is parsed (`core.artifact_readers._sandbox`). The reason already comes
from the confinement layer; the remedy must be as true as the reason. On Windows "Use a macOS host
where the sandbox is available" sends the operator to change computers to attach a file, so off
macOS only the action that works there is offered. A Mac missing its sandbox keeps its wording.

The host is simulated on the exact check the code makes (`platform.system`), inside
`monkeypatch.context()` so it is never left patched. For the macOS case `_SANDBOX_EXEC` points at a
path that does not exist, so a real Mac runner takes the same "sandbox missing" branch.
"""

from __future__ import annotations

import io
import platform
import subprocess
import wave
from pathlib import Path

import pytest

import core.artifact_readers as readers
from core.artifact_readers import _sandbox, archive, office
from core.artifact_readers._sandbox import Scratch, run_confined
from core.artifact_readers._types import ReaderUnavailable
from tests.reader_fixtures import native_text_pdf, rar_archive

OFF_MACOS_REMEDY = "Paste or attach the text you need instead."
#: The macOS wording exactly as shipped before this change. It must not move by a byte.
MAC_REMEDY = "Use a macOS host where the sandbox is available, or attach the content as text."
MAC_PDF_MESSAGE = (
    "report.pdf is PDF, and the macOS sandbox (/usr/bin/sandbox-exec) is not available on this machine. "
    "It was not read: this runtime does not run untrusted document parsers unconfined."
)
MAC_DECODER_MESSAGE = (
    "Reading this file needs an external decoder, and the macOS sandbox (/usr/bin/sandbox-exec) is not "
    "available on this machine. It was not read: this runtime does not run untrusted document parsers unconfined."
)


def _wav() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(8000)
        recording.writeframes(b"\x00\x00" * 800)
    return buffer.getvalue()


#: One file per externally decoded format. Only the leading bytes matter: the refusal comes before
#: anything past format detection is looked at.
EXTERNAL_FORMATS = [
    ("pdf", "report.pdf", native_text_pdf),
    ("doc", "memo.doc", lambda: office.OLE_MAGIC + b"\x00" * 504),
    ("rar", "bundle.rar", lambda: rar_archive({"notes.txt": b"quarterly notes\n"})),
    ("7z", "bundle.7z", lambda: archive.SEVEN_ZIP_MAGIC + b"\x00\x04" + b"\x00" * 56),
    ("audio", "memo.wav", _wav),
    ("video", "clip.mp4", lambda: b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41" + b"\x00" * 4096),
]


def _as_host(patch: pytest.MonkeyPatch, system: str, tmp_path: Path) -> None:
    """Report `system` as the host, with no sandbox binary, and forbid every decoder spawn."""

    def _forbidden(*args, **kwargs):
        raise AssertionError("a decoder was spawned on a host that cannot confine it")

    patch.setattr(platform, "system", lambda: system)
    patch.setattr(_sandbox, "_SANDBOX_EXEC", str(tmp_path / "no-sandbox-exec"))
    patch.setattr(subprocess, "Popen", _forbidden)


@pytest.mark.parametrize(("fmt", "name", "build"), EXTERNAL_FORMATS, ids=[row[0] for row in EXTERNAL_FORMATS])
def test_a_windows_refusal_keeps_the_derived_reason_and_offers_only_a_true_action(
    fmt: str, name: str, build, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = build()
    with monkeypatch.context() as patch:
        _as_host(patch, "Windows", tmp_path)
        reason = _sandbox.confinement_unavailable_reason()
        report = readers.capability_report()
        with pytest.raises(ReaderUnavailable) as caught:
            readers.read(data, name=name, extension=Path(name).suffix)
    refusal = caught.value
    assert refusal.code == "confinement_unavailable" and refusal.format == fmt
    # The reason is the confinement layer's own, not a hard-coded verdict about Windows...
    assert "this host is Windows" in reason
    label = readers.spec_for_format(fmt).label
    assert refusal.message == (
        f"{name} is {label}, and {reason}. It was not read: this runtime does not run untrusted document parsers unconfined."
    )
    # ...and it is the same reason the capability report gives for this format.
    row = next(row for row in report["formats"] if row["format"] == fmt)
    assert row["available"] is False and row["blocked_reason"] == reason
    # What the attachment door shows is message + remediation: no advice to change computers.
    assert refusal.remediation == OFF_MACOS_REMEDY
    assert "macOS host" not in f"{refusal.message} {refusal.remediation}"


def test_the_decoder_door_refuses_on_windows_with_the_same_action_before_spawning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:
        _as_host(patch, "Windows", tmp_path)
        reason = _sandbox.confinement_unavailable_reason()
        with Scratch("vool-test-") as scratch, pytest.raises(ReaderUnavailable) as caught:
            run_confined(["/bin/echo", "hello"], scratch=scratch.path, fmt="pdf")
    assert caught.value.code == "confinement_unavailable"
    assert reason in caught.value.message and "unconfined" in caught.value.message
    assert caught.value.remediation == OFF_MACOS_REMEDY


def test_a_mac_without_its_sandbox_keeps_its_exact_words_and_only_other_hosts_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    refusals: dict[str, ReaderUnavailable] = {}
    for system in ("Darwin", "Windows"):
        with monkeypatch.context() as patch:
            _as_host(patch, system, tmp_path)
            with pytest.raises(ReaderUnavailable) as caught:
                readers.read(native_text_pdf(), name="report.pdf", extension=".pdf")
            refusals[system] = caught.value
            with Scratch("vool-test-") as scratch, pytest.raises(ReaderUnavailable) as decoder:
                run_confined(["/bin/echo", "hello"], scratch=scratch.path, fmt="pdf")
            refusals[system + ":decoder"] = decoder.value
    assert refusals["Darwin"].message == MAC_PDF_MESSAGE
    assert refusals["Darwin"].remediation == MAC_REMEDY
    assert refusals["Darwin:decoder"].message == MAC_DECODER_MESSAGE
    assert refusals["Darwin:decoder"].remediation == MAC_REMEDY
    # The same refusal off macOS changes only where the macOS advice would be untrue.
    assert refusals["Windows"].code == refusals["Darwin"].code == "confinement_unavailable"
    assert refusals["Windows"].remediation == refusals["Windows:decoder"].remediation == OFF_MACOS_REMEDY
