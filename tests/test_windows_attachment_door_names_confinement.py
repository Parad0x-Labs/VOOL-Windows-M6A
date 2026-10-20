"""What the chat attachment door says on Windows when a file needs a decoder this host may not run.

PDF, legacy .doc, RAR, 7z, audio and video are read by external decoders, and those run only
confined. Confinement exists only on macOS (`core.artifact_readers._sandbox`), so on Windows the
upload door refuses such a file before it is staged. It used to say the machine "is missing" the
decoder -- for a PDF, "PDFKit or pypdf", on an install that ships pypdf. What is missing there is
the confinement, which the reader layer already names. On Windows the door now refuses in the
reader's own words, with the remedy that is true on that host. The error code, the HTTP status and
the response shape stay the same; only the words change. macOS and Linux keep their exact bytes.

Requests go through the served application (`apps.vool_api_server.create_app`). That is the raw
body door the composer's file picker and its file paste both post to. They also go through the
mobile companion's `attachments.upload` action, the other caller of the same classification. The
host is simulated on the probe the confinement layer names the host by (`platform.system`), inside
`monkeypatch.context()`, with no sandbox binary, and every process spawn is recorded and refused.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import platform
import subprocess
import urllib.parse
import wave
from collections.abc import Iterator
from pathlib import Path

import pytest

import core.artifact_readers as readers
from core import runtime_paths
from core.artifact_readers import _sandbox, archive, documents, office
from core.artifact_readers._types import ReaderUnavailable
from tests.asgi_harness import asgi_request
from tests.reader_fixtures import legacy_xls_workbook, native_text_pdf, rar_archive

SESSION = "openclaw:eeeeeeeeeeeeeeeeeeee"
UPLOAD = "/api/chat/attachments/upload"
#: The prior change's remedy off macOS: the one action that works on such a host.
OFF_MACOS_REMEDY = "Paste or attach the text you need instead."


def _wav() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(8000)
        recording.writeframes(b"\x00\x00" * 800)
    return buffer.getvalue()


#: One file per externally decoded format, with the door's refusal exactly as it reads on macOS and
#: Linux before and after this change. These literals must not move by a byte.
EXTERNAL_FORMATS = [
    (
        "pdf",
        "report.pdf",
        "application/pdf",
        native_text_pdf,
        "report.pdf is PDF, and this machine is missing a PDF decoder (PDFKit or pypdf), so it cannot be read. "
        "It was not accepted.",
    ),
    (
        "doc",
        "memo.doc",
        "application/msword",
        lambda: office.OLE_MAGIC + b"\x00" * 504,
        "memo.doc is Legacy Word document (.doc), and this machine is missing the macOS textutil converter, "
        "so it cannot be read. It was not accepted.",
    ),
    (
        "rar",
        "bundle.rar",
        "application/vnd.rar",
        lambda: rar_archive({"notes.txt": b"quarterly notes\n"}),
        "bundle.rar is RAR archive, and this machine is missing a RAR-capable libarchive (bsdtar), "
        "so it cannot be read. It was not accepted.",
    ),
    (
        "7z",
        "bundle.7z",
        "application/x-7z-compressed",
        lambda: archive.SEVEN_ZIP_MAGIC + b"\x00\x04" + b"\x00" * 56,
        "bundle.7z is 7z archive, and this machine is missing a 7z-capable libarchive (bsdtar), "
        "so it cannot be read. It was not accepted.",
    ),
    (
        "audio",
        "memo.wav",
        "audio/x-wav",
        _wav,
        "memo.wav is Audio, and this machine is missing the local on-device speech recogniser "
        "(macOS Speech framework), so it cannot be read. It was not accepted.",
    ),
    (
        "video",
        "clip.mp4",
        "video/mp4",
        lambda: b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41" + b"\x00" * 4096,
        "clip.mp4 is Video, and this machine is missing ffmpeg, so it cannot be read. It was not accepted.",
    ),
]


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    home = tmp_path / "vool-home"
    previous = runtime_paths._VOOL_HOME_OVERRIDE
    monkeypatch.setenv("VOOL_HOME", str(home))
    runtime_paths.configure_runtime_home(home)
    try:
        yield
    finally:
        runtime_paths.configure_runtime_home(previous)


@contextlib.contextmanager
def _host(monkeypatch: pytest.MonkeyPatch, system: str, tmp_path: Path) -> Iterator[list[object]]:
    """Report `system` as the host, with no sandbox binary. Every spawn is recorded and refused."""
    spawned: list[object] = []

    def _refuse_spawn(*args, **kwargs):
        spawned.append(args[0] if args else kwargs.get("args"))
        raise OSError("the test refuses every process spawn: this host cannot confine a decoder")

    with monkeypatch.context() as patch:
        patch.setattr(platform, "system", lambda: system)
        patch.setattr(_sandbox, "_SANDBOX_EXEC", str(tmp_path / "no-sandbox-exec"))
        patch.setattr(subprocess, "Popen", _refuse_spawn)
        yield spawned


def _app():
    from apps.vool_api_server import create_app
    from core.web.api.runtime import RuntimeServices

    return create_app(RuntimeServices(display_name="VOOL"))


def _upload(app, *, name: str, media_type: str, data: bytes) -> tuple[int, dict]:
    """The request the composer sends for a picked or pasted file: no document source header."""
    headers = {
        "Content-Type": "application/octet-stream",
        "X-Vool-Attachment-Name": urllib.parse.quote(name, safe=""),
        "X-Vool-Attachment-Type": media_type,
        "X-Vool-Session-Id": SESSION,
        "Host": "127.0.0.1",
    }
    status, _, body = asgi_request(app, method="POST", path=UPLOAD, headers=headers, body=data)
    return status, json.loads(body or b"{}")


def _staged(app) -> list[dict]:
    status, _, body = asgi_request(
        app, method="GET", path="/api/chat/attachments?session=" + urllib.parse.quote(SESSION), headers={"Host": "127.0.0.1"}
    )
    assert status == 200
    return json.loads(body)["attachments"]


def _reader_words(data: bytes, name: str) -> str:
    """What the reader itself says about this file on the current host: message, then remedy."""
    with pytest.raises(ReaderUnavailable) as caught:
        readers.read(data, name=name, extension=Path(name).suffix)
    assert caught.value.code == "confinement_unavailable"
    return f"{caught.value.message} {caught.value.remediation}"


@pytest.mark.parametrize(
    ("fmt", "name", "media_type", "build", "elsewhere"), EXTERNAL_FORMATS, ids=[row[0] for row in EXTERNAL_FORMATS]
)
def test_on_windows_the_upload_door_names_the_missing_confinement_and_elsewhere_keeps_its_words(
    fmt: str, name: str, media_type: str, build, elsewhere: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = build()
    app = _app()
    with _host(monkeypatch, "Windows", tmp_path) as spawned:
        reason = _sandbox.confinement_unavailable_reason()
        status, payload = _upload(app, name=name, media_type=media_type, data=data)
        reader_says = _reader_words(data, name)
        staged = _staged(app)
    label = readers.spec_for_format(fmt).label
    # The same refusal as before in every field but the words: code, status, shape.
    assert status == 415
    assert set(payload) == {"ok", "error", "message"}
    assert payload["ok"] is False and payload["error"] == f"{fmt}_decoder_unavailable"
    # The words are the reader's own: the confinement layer's reason and the remedy true on Windows.
    assert "this host is Windows" in reason
    assert payload["message"] == (
        f"{name} is {label}, and {reason}. It was not read: this runtime does not run untrusted document parsers "
        f"unconfined. {OFF_MACOS_REMEDY}"
    )
    assert payload["message"] == reader_says
    assert "is missing" not in payload["message"] and "macOS host" not in payload["message"]
    # Refused before anything was spawned or written.
    assert spawned == [] and staged == []

    for system in ("Darwin", "Linux"):
        with _host(monkeypatch, system, tmp_path) as spawned:
            status, payload = _upload(app, name=name, media_type=media_type, data=data)
            staged = _staged(app)
        assert (status, payload) == (415, {"ok": False, "error": f"{fmt}_decoder_unavailable", "message": elsewhere}), system
        assert spawned == [] and staged == [], system


def test_on_windows_a_decoder_that_is_really_missing_is_still_named_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy .xls is read in process by the pinned xlrd, never by a confined decoder. Without xlrd
    the missing decoder IS the cause, on Windows as anywhere, and the door keeps saying so."""
    monkeypatch.setattr(documents, "_import_xrd", lambda: None)
    app = _app()
    with _host(monkeypatch, "Windows", tmp_path) as spawned:
        xls_status, xls = _upload(app, name="q3.xls", media_type="application/vnd.ms-excel", data=legacy_xls_workbook())
        pdf_status, pdf = _upload(app, name="report.pdf", media_type="application/pdf", data=native_text_pdf())
        staged = _staged(app)
    assert (xls_status, xls) == (
        415,
        {
            "ok": False,
            "error": "xls_decoder_unavailable",
            "message": "q3.xls is Legacy Excel workbook (.xls), and this machine is missing the pinned xlrd decoder "
            "(xlrd==2.0.2), so it cannot be read. It was not accepted.",
        },
    )
    # On the same host and in the same session, a format held back by confinement names that instead.
    assert pdf_status == 415 and pdf["error"] == "pdf_decoder_unavailable"
    assert "this host is Windows" in pdf["message"] and "is missing" not in pdf["message"]
    assert spawned == [] and staged == []


def test_the_mobile_companion_upload_refuses_in_the_same_words(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The phone's `attachments.upload` stages through the same classification, so on a Windows
    desktop it gets the same reason and remedy, under the same code and status."""
    from core.web.api.mobile_companion_api import reset_mobile_companion_service
    from core.web.api.runtime import RuntimeServices
    from storage.migrations import run_migrations
    from tests.mobile.test_mobile_companion_api import _body, _companion, _pair

    reset_mobile_companion_service()
    try:
        run_migrations()
        runtime = RuntimeServices(display_name="VOOL")
        claim = _body(_pair(runtime)[0])
        chat_id = _body(_companion(claim, "chats.prepare", runtime=runtime))["result"]["chat_id"]
        data = native_text_pdf()
        params = {
            "chat_id": chat_id,
            "filename": "report.pdf",
            "content_b64": base64.b64encode(data).decode(),
            "media_type": "application/pdf",
            "kind": "file",
        }
        outcomes: dict[str, tuple[int, dict]] = {}
        for system in ("Windows", "Linux"):
            with _host(monkeypatch, system, tmp_path) as spawned:
                response = _companion(claim, "attachments.upload", params, runtime=runtime)
                if system == "Windows":
                    reader_says = _reader_words(data, "report.pdf")
            assert spawned == [], system
            outcomes[system] = (response.status, _body(response)["result"])
    finally:
        reset_mobile_companion_service()
    assert outcomes["Windows"] == (415, {"ok": False, "error": "pdf_decoder_unavailable", "message": reader_says})
    assert "this host is Windows" in reader_says and reader_says.endswith(OFF_MACOS_REMEDY)
    assert outcomes["Linux"] == (415, {"ok": False, "error": "pdf_decoder_unavailable", "message": EXTERNAL_FORMATS[0][4]})
