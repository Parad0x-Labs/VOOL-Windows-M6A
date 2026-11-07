"""The installed shortcuts carry VOOL's icon, and uninstall says what it keeps.

Clean-VM job 52 (port 76afbd36) found two installer defects:

* Blank white shortcut icons. ``[Icons]`` pointed ``IconFilename`` at
  ``{#SourcePath}\\..\\assets\\vool.ico``. ``{#SourcePath}`` is expanded when the installer is
  COMPILED, so the shortcut named a path on the build machine that does not exist on the user's
  PC, and the target (``wscript.exe``) supplies no VOOL icon of its own. The .ico must be installed
  into ``{app}`` and the shortcuts (and the Apps entry) must point there.
* "Completely remove VOOL and all of its components?" then "VOOL was successfully removed", while
  5.23 GB of models, chats, settings and logs stayed in ``%LOCALAPPDATA%\\VOOL`` without a word.
  Keeping that folder is deliberate (models are ``uninsneveruninstall``; no [UninstallDelete]
  reaches it), so the fix is the WORDING: say the program is removed, that the data is kept, and
  where. Nothing may be deleted that was not deleted before.

Inno Setup cannot run here, so these read vool.iss statically, like the other installer tests.
"""
from __future__ import annotations

import re
from pathlib import Path

ISS = Path(__file__).resolve().parent.parent / "installer" / "bundle" / "vool.iss"


def _text() -> str:
    return ISS.read_text(encoding="utf-8")


def _section(name: str) -> list[str]:
    match = re.search(rf"^\[{re.escape(name)}\]\s*$(.*?)(?=^\[|\Z)", _text(), re.MULTILINE | re.DOTALL)
    assert match, f"vool.iss has no [{name}] section"
    return [line.strip() for line in match.group(1).splitlines() if line.strip() and not line.strip().startswith(";")]


def test_the_vool_icon_is_installed_into_the_program_folder() -> None:
    entries = [line for line in _section("Files") if "vool.ico" in line]
    assert entries, "vool.ico is never installed, so no shortcut on the user's PC can show it"
    assert any(
        re.search(r'Source:\s*"\{#SourcePath\}\\\.\.\\assets\\vool\.ico"', line)
        and re.search(r'DestDir:\s*"\{app\}"', line)
        for line in entries
    ), entries


def test_no_shortcut_points_at_a_build_machine_path() -> None:
    for line in _section("Icons"):
        icon = re.search(r'IconFilename:\s*"([^"]+)"', line)
        if icon:
            assert "{#" not in icon.group(1), f"shortcut icon is a compile-time path: {line}"
    launchers = [line for line in _section("Icons") if "vool.vbs" in line]
    assert len(launchers) == 2, launchers
    for line in launchers:
        assert 'IconFilename: "{app}\\vool.ico"' in line, line


def test_the_apps_entry_shows_the_vool_icon() -> None:
    assert re.search(r"^UninstallDisplayIcon=\{app\}\\vool\.ico\s*$", _text(), re.MULTILINE)


def test_uninstall_wording_says_the_data_folder_is_kept() -> None:
    messages = "\n".join(_section("Messages"))
    confirm = re.search(r"^ConfirmUninstall=(.+)$", messages, re.MULTILINE)
    finished = re.search(r"^UninstalledAll=(.+)$", messages, re.MULTILINE)
    assert confirm and finished, "uninstall still uses Inno's 'completely remove ... all of its components'"
    for text in (confirm.group(1), finished.group(1)):
        assert "completely" not in text.lower() and "all of its components" not in text.lower()
        assert "kept" in text.lower(), text


def test_the_finished_note_names_the_real_kept_folder() -> None:
    code = _text().split("[Code]", 1)[1]
    step = re.search(r"procedure CurUninstallStepChanged\(CurUninstallStep: TUninstallStep\);(.*?)\nend;", code, re.DOTALL)
    assert step, "no post-uninstall note naming the kept folder"
    body = step.group(1)
    assert "usPostUninstall" in body
    assert "ExpandConstant('{localappdata}\\VOOL')" in body
    assert "UninstallSilent" in body, "a silent uninstall must not stop on a message box"
    assert "MsgBox" in body


def test_the_wording_change_deletes_nothing_more() -> None:
    code = _text().split("[Code]", 1)[1]
    for forbidden in ("DelTree", "DeleteFile", "RemoveDir"):
        assert forbidden not in code, f"uninstall code gained a delete: {forbidden}"
    assert _section("UninstallDelete") == ['Type: filesandordirs; Name: "{app}\\app"']
