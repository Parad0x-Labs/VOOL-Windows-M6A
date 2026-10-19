"""The Media Studio editor's export asks the Windows write gate about the files it writes.

The editor (`/media-editor`, core.web.api.media_editor_api) exports through POST /media-editor/export,
whose export_project writes the output, a stage file beside it for each edit, and the folders that
hold them. On Windows VOOL changes the user's files only inside the folders listed in
VOOL_WINDOWS_DISPOSABLE_WORKSPACES (core.windows_write_gate). The media.export tool was refused
outside them, but the editor's route never asked the gate: it answered 200 "exported" and VOOL
created the output's folder, and asked the media worker to write there, outside every flagged folder.

The media worker is a process of its own that needs ffmpeg, so a recorder of the files VOOL asks it
to write stands in for it. The Windows decision is driven on every platform through the gate's
_IS_WINDOWS; the Windows gauntlet runs this file on Windows too.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import windows_write_gate as gate
from tests.asgi_harness import asgi_request

HEADERS = {"Content-Type": "application/json", "Host": "127.0.0.1:11435"}


@pytest.fixture
def editor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """The served app, a project with one edit, and the files the export asks the worker to write."""
    from apps.vool_api_server import create_app
    from core import media_studio_service as service
    from core.web.api.runtime import RuntimeServices

    asked: list[str] = []

    def worker(op: str, params: dict, *, timeout: float | None = None) -> dict:
        if "dst" in params:
            asked.append(params["dst"])
        return {"duration": 6.0, "width": 320, "height": 240} if op == "probe" else {}

    monkeypatch.setattr(service, "_worker_call", worker)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    project = service.open_project(str(source))
    service.apply_edit(project.project_id, "trim", {"start": 1.0, "end": 2.0}, actor="gui")
    flagged = tmp_path / "flagged"
    outside = tmp_path / "outside"
    flagged.mkdir()
    outside.mkdir()
    return SimpleNamespace(
        app=create_app(RuntimeServices(display_name="VOOL")),
        project_id=project.project_id,
        asked=asked,
        flagged=flagged,
        outside=outside,
    )


def _request(editor: SimpleNamespace, method: str, path: str, payload: dict) -> tuple[int, dict]:
    status, _headers, raw = asgi_request(
        editor.app, method=method, path=path, headers=HEADERS, body=json.dumps(payload).encode()
    )
    return status, json.loads(raw)


def _post(editor: SimpleNamespace, path: str, payload: dict) -> tuple[int, dict]:
    return _request(editor, "POST", path, payload)


def _export(editor: SimpleNamespace, output_path: str) -> tuple[int, dict]:
    return _post(editor, "/media-editor/export", {"project_id": editor.project_id, "output_path": output_path})


def _on_windows(monkeypatch: pytest.MonkeyPatch, *flagged: Path) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    if flagged:
        monkeypatch.setenv(gate.ENV_VAR, os.pathsep.join(str(root) for root in flagged))
    else:
        monkeypatch.delenv(gate.ENV_VAR, raising=False)


def _home(monkeypatch: pytest.MonkeyPatch, folder: Path) -> None:
    # `~` is the user's home: HOME on POSIX, USERPROFILE on Windows.
    monkeypatch.setenv("HOME", str(folder))
    monkeypatch.setenv("USERPROFILE", str(folder))


def test_an_editor_export_outside_the_flagged_folders_is_refused_and_writes_nothing(
    editor: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on_windows(monkeypatch, editor.flagged)
    new_folder = editor.outside / "exports"

    status, payload = _export(editor, str(new_folder / "clip.mp4"))

    assert status == 403, payload
    assert payload["ok"] is False and payload["error"] == gate.STATUS, payload
    assert "nothing was changed" in payload["message"], payload
    assert editor.asked == []
    assert not new_folder.exists()


def test_with_no_flagged_folder_every_editor_export_is_refused(
    editor: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on_windows(monkeypatch)

    status, payload = _export(editor, str(editor.flagged / "clip.mp4"))

    assert status == 403 and payload["error"] == gate.STATUS, payload
    assert "No folder is marked" in payload["message"], payload
    assert editor.asked == []


def test_an_editor_export_inside_a_flagged_folder_still_exports_there(
    editor: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on_windows(monkeypatch, editor.flagged)
    new_folder = editor.flagged / "exports"

    status, payload = _export(editor, str(new_folder / "clip.mp4"))

    assert status == 200 and payload["receipt"]["status"] == "exported", payload
    # A stage file for the trim, then the output, both in the folder the gate checked.
    assert len(editor.asked) == 2, editor.asked
    assert all(Path(dst).parent.resolve() == new_folder.resolve() for dst in editor.asked), editor.asked
    assert Path(editor.asked[-1]).resolve() == (new_folder / "clip.mp4").resolve()


def test_a_relative_editor_export_path_is_checked_where_the_export_writes_it(
    editor: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # export_project takes a relative output path from VOOL's working directory, not from any flagged folder.
    _on_windows(monkeypatch, editor.flagged)

    monkeypatch.chdir(editor.outside)
    status, payload = _export(editor, "clip.mp4")
    assert status == 403 and payload["error"] == gate.STATUS, payload
    assert editor.asked == []

    monkeypatch.chdir(editor.flagged)
    status, payload = _export(editor, "clip.mp4")
    assert status == 200 and payload["receipt"]["status"] == "exported", payload
    assert Path(editor.asked[-1]).resolve() == (editor.flagged / "clip.mp4").resolve()


def test_a_home_editor_export_path_is_checked_where_the_export_writes_it(
    editor: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # export_project expands `~` in the user's home, wherever VOOL's working directory is.
    _on_windows(monkeypatch, editor.flagged)

    _home(monkeypatch, editor.outside)
    monkeypatch.chdir(editor.flagged)
    status, payload = _export(editor, "~/exports/clip.mp4")
    assert status == 403 and payload["error"] == gate.STATUS, payload
    assert editor.asked == []
    assert not (editor.outside / "exports").exists()

    _home(monkeypatch, editor.flagged)
    monkeypatch.chdir(editor.outside)
    status, payload = _export(editor, "~/exports/clip.mp4")
    assert status == 200 and payload["receipt"]["status"] == "exported", payload
    assert Path(editor.asked[-1]).resolve() == (editor.flagged / "exports" / "clip.mp4").resolve()


@pytest.mark.parametrize(
    ("output_path", "home", "cwd"),
    [
        # A leading space: a folder named " ~" in VOOL's working directory, not the home.
        (" ~/exports/clip.mp4", "flagged", "outside"),
        # A dollar sign: a folder named "$HOME" in VOOL's working directory, not the variable.
        ("$HOME/exports/clip.mp4", "flagged", "outside"),
        # A parent step after `~` climbs out of the home, not out of the working directory.
        ("~/../exports/clip.mp4", "outside/home", "flagged"),
    ],
    ids=["leading-space", "dollar-sign", "parent-of-home"],
)
def test_an_editor_export_path_is_checked_as_typed(
    editor: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, output_path: str, home: str, cwd: str
) -> None:
    # export_project neither strips the path, expands environment variables nor folds "..", so the gate
    # must place the same text the same way. Each of these lands outside the flagged folder.
    root = editor.flagged.parent
    (root / home).mkdir(parents=True, exist_ok=True)
    _on_windows(monkeypatch, editor.flagged)
    _home(monkeypatch, root / home)
    monkeypatch.chdir(root / cwd)
    before = sorted(editor.outside.rglob("*")) + sorted(editor.flagged.rglob("*"))

    status, payload = _export(editor, output_path)

    assert status == 403 and payload["error"] == gate.STATUS, payload
    assert editor.asked == []
    assert sorted(editor.outside.rglob("*")) + sorted(editor.flagged.rglob("*")) == before


@pytest.mark.parametrize("output_path", ["", "   "], ids=["empty", "blank"])
def test_an_editor_export_with_no_output_path_is_refused(
    editor: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, output_path: str
) -> None:
    # An empty path places the export at VOOL's working directory itself, its stage files in the folder above.
    _on_windows(monkeypatch, editor.flagged)
    monkeypatch.chdir(editor.flagged)

    status, payload = _export(editor, output_path)

    assert status == 403 and payload["error"] == gate.STATUS, payload
    assert "names no output_path" in payload["message"], payload
    assert editor.asked == []


def test_off_windows_the_editor_export_is_unchanged(editor: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    monkeypatch.delenv(gate.ENV_VAR, raising=False)

    status, payload = _export(editor, str(editor.outside / "clip.mp4"))

    assert status == 200 and payload["receipt"]["status"] == "exported", payload
    assert Path(editor.asked[-1]).resolve() == (editor.outside / "clip.mp4").resolve()


def test_the_editor_export_command_writes_nothing_outside_the_flagged_folders(
    editor: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same export is registered as the command media.editor.export, served at /api/commands/dispatch.
    Whatever that door answers, on Windows it must not write outside the flagged folders. Today it fails
    before the export (its handler passes `client_host`, which handle_media_editor_post does not take), so
    this passes without the gate too: it guards the day that door is fixed."""
    _on_windows(monkeypatch, editor.flagged)
    new_folder = editor.outside / "exports"

    status, payload = _post(editor, "/api/commands/dispatch", {
        "command_id": "media.editor.export",
        "input": {"body": {"project_id": editor.project_id, "output_path": str(new_folder / "clip.mp4")}},
    })

    assert payload.get("ok") is not True, (status, payload)
    assert editor.asked == []
    assert not new_folder.exists()


def test_a_delete_to_the_editor_export_writes_nothing_outside_the_flagged_folders(
    editor: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The served app hands a DELETE to the same dispatcher as a POST (core/web/api/app.py), so a DELETE to
    /media-editor/export reaches the export too. Whatever it answers, on Windows it must not write outside
    the flagged folders."""
    _on_windows(monkeypatch, editor.flagged)
    new_folder = editor.outside / "exports"

    status, payload = _request(editor, "DELETE", "/media-editor/export", {
        "project_id": editor.project_id, "output_path": str(new_folder / "clip.mp4"),
    })

    assert payload.get("ok") is not True, (status, payload)
    assert editor.asked == []
    assert not new_folder.exists()
