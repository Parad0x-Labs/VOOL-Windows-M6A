"""The Windows installer starts, and still stops VOOL before it replaces or removes files.

Inno Setup sets {app} only once the destination folder is known. The installer's stop
(StopNulla) expands {app} to match only this installation's processes, and calling it from
InitializeSetup aborted every install before it began: "An attempt was made to expand the "app"
constant before it was initialized", Setup exit code 1, nothing installed. The install-time stop
runs from PrepareToInstall instead, after the destination is chosen and before any file is
touched; the uninstaller knows {app} from the start, so InitializeUninstall keeps its stop.
"""

from __future__ import annotations

import re
from pathlib import Path

ISS = Path(__file__).resolve().parent.parent / "installer" / "bundle" / "vool.iss"
# Event functions Inno Setup calls before the destination folder, and so {app}, is known.
BEFORE_DESTINATION = ("InitializeSetup", "InitializeWizard")
ROUTINE = re.compile(r"^\s*(?:procedure|function)\s+(\w+)", re.IGNORECASE | re.MULTILINE)


def _scan_code() -> tuple[str, str]:
    """The [Code] section with comments blanked: once with string literals kept, once blanked.

    Both copies keep every offset, so a routine found in one slices the same text in the other.
    The literals hold PowerShell braces, and the comments hold apostrophes, so neither can be
    stripped with a plain pattern.
    """
    text = ISS.read_text(encoding="utf-8")
    start = re.search(r"^\[Code\]\s*$", text, re.MULTILINE)
    assert start, "vool.iss has no [Code] section"
    following = re.search(r"^\[\w+\]\s*$", text[start.end() :], re.MULTILINE)
    code = text[start.end() : start.end() + following.start()] if following else text[start.end() :]
    kept: list[str] = []
    blank: list[str] = []
    i = 0
    while i < len(code):
        if code[i] == "'":
            end = i + 1
            while end < len(code):
                if code[end] == "'" and code[end + 1 : end + 2] == "'":
                    end += 2  # a doubled quote inside the literal
                elif code[end] == "'":
                    break
                else:
                    end += 1
            literal = code[i : end + 1]
            kept.append(literal)
            blank.append(re.sub(r"[^\n]", " ", literal))
            i = end + 1
            continue
        for opener, closer in (("{", "}"), ("(*", "*)"), ("//", "\n")):
            if code.startswith(opener, i):
                end = code.find(closer, i + len(opener))
                end = len(code) if end < 0 else end + len(closer)
                filler = re.sub(r"[^\n]", " ", code[i:end])
                kept.append(filler)
                blank.append(filler)
                i = end
                break
        else:
            kept.append(code[i])
            blank.append(code[i])
            i += 1
    return "".join(kept), "".join(blank)


def _routines() -> dict[str, tuple[str, str]]:
    """Each [Code] routine by lower-case name: (its text with literals, its text without)."""
    kept, blank = _scan_code()
    headers = list(ROUTINE.finditer(blank))
    routines: dict[str, tuple[str, str]] = {}
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(blank)
        routines[header.group(1).lower()] = (kept[header.end() : end], blank[header.end() : end])
    return routines


def _reachable(event: str, routines: dict[str, tuple[str, str]]) -> set[str]:
    seen: set[str] = set()
    pending = [event.lower()]
    while pending:
        name = pending.pop()
        if name in seen or name not in routines:
            continue
        seen.add(name)
        calls = routines[name][1]
        pending.extend(other for other in routines if re.search(rf"\b{other}\b", calls, re.IGNORECASE))
    return seen


def test_setup_never_expands_the_install_folder_before_it_is_chosen() -> None:
    routines = _routines()
    assert "stopnulla" in routines
    for event in BEFORE_DESTINATION:
        for name in _reachable(event, routines):
            body = routines[name][0].lower()
            assert "{app}" not in body, f"{event} reaches {name}, which expands {{app}} before it is set"
            assert "wizarddirvalue" not in body, f"{event} reaches {name}, which reads the unset destination"


def test_install_and_uninstall_still_stop_vool_before_touching_files() -> None:
    routines = _routines()
    assert "stopnulla" in _reachable("PrepareToInstall", routines)
    assert "stopnulla" in _reachable("InitializeUninstall", routines)
