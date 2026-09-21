from __future__ import annotations

import pytest

from core.artifact_readers import REGISTRY, image, pdf, speech_tool, vision_tool
from core.artifact_readers._types import ReaderUnavailable


def _assert_not_mac_only(*texts: str) -> None:
    combined = " ".join(texts).lower()
    assert "swift" not in combined
    assert "xcode" not in combined
    assert "xcode-select" not in combined


def test_windows_speech_absence_is_honest_and_actionable(monkeypatch):
    monkeypatch.setattr(speech_tool.platform, "system", lambda: "Windows")

    unavailable = speech_tool.transcribe_unavailable()

    assert unavailable is not None
    assert unavailable.code == "speech_toolchain_unavailable"
    assert "Windows build" in unavailable.message
    _assert_not_mac_only(unavailable.message, unavailable.remediation)
    with pytest.raises(ReaderUnavailable) as caught:
        speech_tool.ensure_tool()
    _assert_not_mac_only(caught.value.message, caught.value.remediation)


def test_windows_vision_absence_is_honest_and_actionable(monkeypatch):
    monkeypatch.setattr(vision_tool.platform, "system", lambda: "Windows")

    message, remediation = vision_tool.unavailable_reason()

    assert "Windows build" in message
    _assert_not_mac_only(message, remediation)
    with pytest.raises(ReaderUnavailable) as caught:
        vision_tool.ensure_tool()
    _assert_not_mac_only(caught.value.message, caught.value.remediation)


def test_windows_image_and_pdf_paths_do_not_offer_mac_prerequisites(monkeypatch):
    monkeypatch.setattr(vision_tool.platform, "system", lambda: "Windows")
    monkeypatch.setattr(image, "ocr_available", lambda: False)
    monkeypatch.setattr(image, "dimensions", lambda _data: (1, 1))

    with pytest.raises(ReaderUnavailable) as image_error:
        image.read(b"not-an-image", name="scan.png")
    _assert_not_mac_only(image_error.value.message, image_error.value.remediation)

    monkeypatch.setattr(pdf.platform, "system", lambda: "Windows")
    _assert_not_mac_only(pdf._pdf_decoder_remediation())


def test_audio_capability_row_does_not_claim_a_mac_only_dependency():
    audio_row = next(spec for spec in REGISTRY if spec.fmt == "audio")
    assert audio_row.requires == ("the local on-device speech recogniser",)
