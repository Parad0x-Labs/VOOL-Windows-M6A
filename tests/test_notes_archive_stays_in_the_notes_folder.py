"""Archiving and restoring a note, and its event receipt, change only real note files in the notes folder.

A user says "archive the note X" or "restore the note X" in chat, and approves a calendar event made from
a note's action line, after which VOOL writes the event's receipt into that note. Each of these writes or
removes files under the workspace's ``notes/`` folder. This file pins, through the real operator route
(the chat parser and the operator dispatcher), that archiving and restoring never reach past a link or lose
another editor's change, and that ``link_note_to_event`` refuses every path the calendar route never stores:

* a ``notes/.archive`` that is a link (to a folder outside the workspace, or to another workspace folder)
  is refused for archive and for restore with a typed status, and nothing behind it is written or removed;
* an archived entry that is a link (to a file outside or inside the workspace) or a folder is not
  restored, read into the notes folder or removed;
* a note that is a link to another workspace file is not archived (the file behind it stays);
* an archive name that is already taken, or is taken by another editor while the note is archived, is
  refused and both entries stay as they were;
* a note (or an archived note) that another editor changes while it is moved stays where it was with that
  change, and the new copy is removed (``archive_note_changed`` / ``restore_note_changed``); when the new
  copy is changed too, both files stay with their changes and the answer names both
  (``archive_move_incomplete`` / ``restore_move_incomplete``);
* ``link_note_to_event`` writes no receipt into a ``.txt`` file, a note in a subfolder or in ``.archive``,
  a path that climbs out of the notes folder, or a note outside the workspace;
* a plain note still archives and restores with the same bytes.

The receipt the calendar route writes after an approved event is pinned, with a real loopback CalDAV
server, in ``tests/test_note_event_receipt_stays_in_the_note.py``, which reuses this file's notes world and
helpers.

The concurrent changes are staged by wrapping the real writers and remover in
``core.execution.artifacts``: each wrapper calls the real function and, at the chosen moment, appends to
a file (or writes a new one) the way another editor saving it would.

Directory links are junctions on Windows (no privilege needed) and symbolic links elsewhere; file links
are symbolic links on every platform. A host that cannot make a file symbolic link (Windows without
Developer Mode or an elevated shell) FAILS the file-link cases, naming that cause; they are never skipped.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import fs_links

CREATED = "2026-10-01T09:00:00+00:00"
EDIT = b"changed meanwhile by another editor\n"
COPY_EDIT = b"the new copy changed meanwhile too\n"


def _note_bytes(title: str, body: str, *, extra: str = "") -> bytes:
    """A note exactly as `create_note` lays one out (front matter, a blank line, the body)."""
    return (f"---\ntitle: {title}\ncreated: {CREATED}\nupdated: {CREATED}\nsession: notes-owner\n{extra}---\n\n"
            f"{body}\n").encode()


def _stamp_line(data: bytes, key: str) -> bytes:
    """The ``key: <time>`` front-matter line the code under test wrote, checked to be a real, zoned time."""
    line = next(line for line in data.split(b"\n") if line.startswith(f"{key}: ".encode()))
    stamped = datetime.fromisoformat(line.removeprefix(f"{key}: ".encode()).decode())
    assert stamped.tzinfo is not None and stamped >= datetime.fromisoformat(CREATED), line
    return line


def _as_archived(original: bytes, archived_at: bytes) -> bytes:
    """``original`` (a `_note_bytes` note without extra fields) as archiving writes it."""
    return original.replace(b"session: notes-owner\n", b"session: notes-owner\n" + archived_at + b"\n")


def _as_restored(unarchived: bytes, updated: bytes) -> bytes:
    """``unarchived`` (the archived note without its archived_at line) as restoring writes it."""
    return unarchived.replace(f"updated: {CREATED}".encode(), updated)


def _dir_link(link: Path, target: Path) -> None:
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)
    assert fs_links.is_link(link)


def _file_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.fail(f"this host cannot create a file symbolic link ({exc}); on Windows that needs Developer Mode "
                    "or an elevated shell, so the case it sets up is unproven here, not passed")
    assert fs_links.is_link(link)


def _tree(root: Path) -> dict[str, object]:
    """Every entry under ``root`` by relative path: a link as where it points (never followed), a folder as
    "dir", a file as its bytes."""
    found: dict[str, object] = {}

    def walk(folder: Path, prefix: str) -> None:
        for entry in sorted(folder.iterdir(), key=lambda item: item.name):
            name = f"{prefix}{entry.name}"
            if fs_links.is_link(entry):
                found[name] = ("link", os.readlink(entry))
            elif entry.is_dir():
                found[name] = "dir"
                walk(entry, f"{name}/")
            else:
                found[name] = entry.read_bytes()

    if root.is_dir():
        walk(root, "")
    return found


def _append(path: Path, extra: bytes) -> None:
    """Another editor adds a line to ``path`` and saves it."""
    with path.open("ab") as handle:
        handle.write(extra)


Stage = tuple[Callable[[Path], bool], Callable[[Path], None]]


def _stage_edits(monkeypatch: pytest.MonkeyPatch, *, before_write: Stage | None = None,
                 after_write: Stage | None = None, before_remove: Stage | None = None) -> list[str]:
    """Run the REAL writers and remover the note code calls, with another editor acting at chosen moments.

    Each moment is ``(which, edit)``: the first time the note code writes (or removes) a path for which
    ``which(path)`` holds, ``edit(path)`` runs just before or just after the real call. Both writers are
    wrapped (the committed code writes with ``pinned_atomic_write_text``, 1c26068 wrote with
    ``atomic_write_text``) and the note code looks them up at call time, so the same change lands on either.
    Returns the moments that fired, in order."""
    from core.execution import artifacts

    moments = {"before_write": before_write, "after_write": after_write, "before_remove": before_remove}
    fired: list[str] = []

    def at(moment: str, path: object) -> None:
        stage = moments[moment]
        if stage is not None and moment not in fired and stage[0](Path(str(path))):
            fired.append(moment)
            stage[1](Path(str(path)))

    def wrapped_writer(real: Callable[..., object]) -> Callable[..., object]:
        def write(path, content, *args, **kwargs):
            at("before_write", path)
            written = real(path, content, *args, **kwargs)
            at("after_write", path)
            return written

        return write

    def wrapped_remover(real: Callable[..., object]) -> Callable[..., object]:
        def remove(path, *args, **kwargs):
            at("before_remove", path)
            return real(path, *args, **kwargs)

        return remove

    for name in ("pinned_atomic_write_text", "atomic_write_text"):
        monkeypatch.setattr(artifacts, name, wrapped_writer(getattr(artifacts, name)))
    monkeypatch.setattr(artifacts, "pinned_remove_created_file", wrapped_remover(artifacts.pinned_remove_created_file))
    return fired


def _say(text: str, *, session_id: str = "notes-landing"):
    """One chat request through the operator lane: the parser, then the dispatcher a chat turn calls."""
    from core.local_operator_actions import dispatch_operator_action, parse_operator_action_intent

    intent = parse_operator_action_intent(text)
    assert intent is not None, f"the operator parser did not claim {text!r}"
    return intent, dispatch_operator_action(intent, task_id=f"task-{uuid.uuid4().hex[:8]}", session_id=session_id)


def _archive(title: str):
    intent, result = _say(f'archive the note "{title}"')
    assert (intent.kind, intent.target_label) == ("archive_note", title)
    return result


def _restore(title: str):
    intent, result = _say(f'restore the note "{title}"')
    assert (intent.kind, intent.target_label) == ("restore_note", title)
    return result


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    base = tmp_path.resolve()
    workspace, outside = base / "workspace", base / "outside"
    notes, docs = workspace / "notes", workspace / "docs"
    for folder in (notes, docs, outside):
        folder.mkdir(parents=True)
    monkeypatch.setenv("VOOL_HOME", str(base / "home"))
    monkeypatch.setenv("VOOL_WORKSPACE_ROOT", str(workspace))
    from core import runtime_paths

    monkeypatch.setattr(runtime_paths, "_VOOL_HOME_OVERRIDE", None, raising=False)
    from storage.migrations import run_migrations

    run_migrations()
    return SimpleNamespace(workspace=workspace, notes=notes, docs=docs, outside=outside)


def _refused(result, status: str) -> None:
    assert not result.ok, result.response_text
    assert result.status == "failed", result.response_text
    assert result.details.get("status") == status, result.details


# --- archive and restore through a linked notes/.archive ----------------------------------------------


def test_archiving_through_an_archive_folder_linked_outside_the_workspace_is_refused_and_the_note_stays(world) -> None:
    note = world.notes / "kickoff.md"
    note.write_bytes(_note_bytes("Kickoff", "agenda: intro and goals"))
    _dir_link(world.notes / ".archive", world.outside)
    workspace_before = _tree(world.workspace)

    result = _archive("Kickoff")

    assert _tree(world.outside) == {}, "nothing was written outside the workspace"
    assert _tree(world.workspace) == workspace_before, "the note and the link both stay as they were"
    assert note.read_bytes() == _note_bytes("Kickoff", "agenda: intro and goals")
    _refused(result, "archive_unconfined")


def test_archiving_through_an_archive_folder_linked_to_another_workspace_folder_is_refused(world) -> None:
    (world.docs / "readme.md").write_bytes(b"# project docs\n")
    note = world.notes / "kickoff.md"
    note.write_bytes(_note_bytes("Kickoff", "agenda: intro and goals"))
    _dir_link(world.notes / ".archive", world.docs)
    workspace_before = _tree(world.workspace)

    result = _archive("Kickoff")

    assert _tree(world.workspace) == workspace_before, "neither the docs folder nor the note changed"
    assert sorted(path.name for path in world.docs.iterdir()) == ["readme.md"]
    assert _tree(world.outside) == {}
    _refused(result, "archive_unconfined")


@pytest.mark.parametrize("behind", ["outside", "docs"])
def test_restoring_through_a_linked_archive_folder_neither_restores_nor_removes_the_file_behind_it(world, behind) -> None:
    folder = getattr(world, behind)
    archived_looking = folder / "kickoff.md"
    archived_looking.write_bytes(_note_bytes("Kickoff", "the file behind the link", extra=f"archived_at: {CREATED}\n"))
    _dir_link(world.notes / ".archive", folder)
    workspace_before, outside_before = _tree(world.workspace), _tree(world.outside)

    result = _restore("Kickoff")

    assert archived_looking.read_bytes() == _note_bytes("Kickoff", "the file behind the link",
                                                        extra=f"archived_at: {CREATED}\n")
    assert _tree(world.workspace) == workspace_before, "no note was restored and nothing was removed"
    assert _tree(world.outside) == outside_before
    _refused(result, "archive_unconfined")


# --- archived entries and notes that are links or not files --------------------------------------------


@pytest.mark.parametrize("behind", ["outside", "docs"])
def test_restoring_does_not_follow_an_archived_entry_that_links_to_another_file(world, behind) -> None:
    linked = getattr(world, behind) / "kickoff.md"
    linked.write_bytes(_note_bytes("Kickoff", "text behind the link that must not become a note",
                                   extra=f"archived_at: {CREATED}\n"))
    (world.notes / ".archive").mkdir()
    _file_link(world.notes / ".archive" / "kickoff.md", linked)
    workspace_before, outside_before = _tree(world.workspace), _tree(world.outside)

    result = _restore("Kickoff")

    assert linked.read_bytes() == _note_bytes("Kickoff", "text behind the link that must not become a note",
                                              extra=f"archived_at: {CREATED}\n"), "the linked file is neither removed nor changed"
    assert _tree(world.outside) == outside_before
    assert _tree(world.workspace) == workspace_before, "the link stays and no note was made from what it points at"
    _refused(result, "not_found")


def test_restoring_skips_an_archived_entry_that_is_a_folder(world) -> None:
    folder = world.notes / ".archive" / "kickoff.md"
    folder.mkdir(parents=True)
    (folder / "inside.md").write_bytes(_note_bytes("Kickoff", "a file inside a folder named like a note"))
    workspace_before = _tree(world.workspace)

    result = _restore("Kickoff")

    assert _tree(world.workspace) == workspace_before, "the folder and what it holds stay, and no note was made"
    _refused(result, "not_found")


def test_an_archived_link_does_not_shadow_the_real_archived_note_of_the_same_title(world) -> None:
    secret = world.outside / "kickoff.md"
    secret.write_bytes(_note_bytes("Kickoff", "outside text", extra=f"archived_at: {CREATED}\n"))
    archive = world.notes / ".archive"
    archive.mkdir()
    _file_link(archive / "a-kickoff.md", secret)
    real = archive / "kickoff.md"
    real.write_bytes(_note_bytes("Kickoff", "the real archived agenda", extra=f"archived_at: {CREATED}\n"))
    link_entry, outside_before = _tree(archive)["a-kickoff.md"], _tree(world.outside)

    result = _restore("Kickoff")

    assert _tree(archive) == {"a-kickoff.md": link_entry}, "the link entry is left alone and the real copy moved back"
    assert _tree(world.outside) == outside_before
    assert result.ok and result.status == "executed", result.response_text
    restored = Path(result.details["note_path"])
    assert restored.parent == world.notes
    assert sorted(path.name for path in world.notes.iterdir()) == sorted([".archive", restored.name])
    back = restored.read_bytes()
    assert back == _as_restored(_note_bytes("Kickoff", "the real archived agenda"), _stamp_line(back, "updated")), \
        "the restored note is the real archived note, without its archive stamp and with a new updated stamp"


def test_archiving_a_note_that_links_to_another_workspace_file_is_refused_and_the_file_stays(world) -> None:
    plan = world.docs / "plan.md"
    plan.write_bytes(_note_bytes("Plan", "release plan kept in docs"))
    _file_link(world.notes / "plan.md", plan)
    workspace_before = _tree(world.workspace)

    result = _archive("Plan")

    assert plan.read_bytes() == _note_bytes("Plan", "release plan kept in docs"), "the linked workspace file stays"
    assert _tree(world.workspace) == workspace_before, "the link stays and no archive folder or copy was made"
    assert not os.path.lexists(world.notes / ".archive")
    _refused(result, "not_a_plain_note")


# --- the archive name is taken -------------------------------------------------------------------------


def test_an_archive_name_already_taken_is_refused_and_both_files_stay(world) -> None:
    note = world.notes / "kickoff.md"
    note.write_bytes(_note_bytes("Kickoff", "this week's agenda"))
    (world.notes / ".archive").mkdir()
    older = world.notes / ".archive" / "kickoff.md"
    older.write_bytes(_note_bytes("Kickoff", "last week's agenda", extra=f"archived_at: {CREATED}\n"))
    workspace_before = _tree(world.workspace)

    result = _archive("Kickoff")

    assert note.read_bytes() == _note_bytes("Kickoff", "this week's agenda")
    assert older.read_bytes() == _note_bytes("Kickoff", "last week's agenda", extra=f"archived_at: {CREATED}\n")
    assert _tree(world.workspace) == workspace_before
    _refused(result, "archive_name_taken")


def test_an_archive_name_taken_while_the_note_is_archived_is_refused_and_both_files_stay(world, monkeypatch) -> None:
    original = _note_bytes("Kickoff", "this week's agenda")
    other = _note_bytes("Kickoff", "archived by another editor meanwhile", extra=f"archived_at: {CREATED}\n")
    note, copy = world.notes / "kickoff.md", world.notes / ".archive" / "kickoff.md"
    note.write_bytes(original)
    fired = _stage_edits(monkeypatch, before_write=(lambda path: path == copy, lambda path: path.write_bytes(other)))

    result = _archive("Kickoff")

    assert fired == ["before_write"], "another editor archived a note of that name just before the copy was written"
    assert _tree(world.workspace) == {"docs": "dir", "notes": "dir", "notes/.archive": "dir",
                                      "notes/.archive/kickoff.md": other, "notes/kickoff.md": original}
    _refused(result, "archive_name_taken")
    assert result.details["detail"] == "kickoff.md stays in the notes folder"

def test_an_archive_name_held_by_a_link_is_refused_and_the_link_is_not_replaced(world) -> None:
    note = world.notes / "kickoff.md"
    note.write_bytes(_note_bytes("Kickoff", "this week's agenda"))
    (world.notes / ".archive").mkdir()
    _file_link(world.notes / ".archive" / "kickoff.md", world.outside / "not-there-yet.md")
    workspace_before = _tree(world.workspace)

    result = _archive("Kickoff")

    assert _tree(world.workspace) == workspace_before, "the note stays and the link entry is not replaced"
    assert _tree(world.outside) == {}
    _refused(result, "archive_name_taken")


# --- the real-folder round trip ------------------------------------------------------------------------


def test_a_plain_note_archives_and_restores_with_the_same_bytes(world) -> None:
    original = _note_bytes("Kickoff", "agenda: intro and goals\n[action] none yet")
    note = world.notes / "kickoff.md"
    note.write_bytes(original)

    archived = _archive("Kickoff")

    assert archived.ok and archived.status == "executed", archived.response_text
    copy = world.notes / ".archive" / "kickoff.md"
    assert Path(archived.details["note_path"]) == copy
    assert not os.path.lexists(note), "the original is gone after archiving"
    stamped = copy.read_bytes()
    assert stamped == _as_archived(original, _stamp_line(stamped, "archived_at"))
    assert [path.name for path in world.notes.glob("*.md")] == []

    restored_result = _restore("Kickoff")

    assert restored_result.ok and restored_result.status == "executed", restored_result.response_text
    assert not os.path.lexists(copy), "the archive copy is gone after restoring"
    restored = Path(restored_result.details["note_path"])
    assert restored.parent == world.notes and [path.name for path in world.notes.glob("*.md")] == [restored.name]
    back = restored.read_bytes()
    assert back == _as_restored(original, _stamp_line(back, "updated")), \
        "the restored note is the original byte for byte, apart from its updated stamp"


# --- another editor changes a file while it is moved ---------------------------------------------------


def test_a_note_changed_while_it_is_archived_keeps_the_change_and_no_copy_is_left(world, monkeypatch) -> None:
    original = _note_bytes("Kickoff", "agenda: intro and goals")
    note, copy = world.notes / "kickoff.md", world.notes / ".archive" / "kickoff.md"
    note.write_bytes(original)
    fired = _stage_edits(monkeypatch, after_write=(lambda path: path == copy, lambda _path: _append(note, EDIT)))

    result = _archive("Kickoff")

    assert fired == ["after_write"], "the note was changed right after its archived copy was written"
    assert _tree(world.workspace) == {"docs": "dir", "notes": "dir", "notes/.archive": "dir",
                                      "notes/kickoff.md": original + EDIT}, \
        "the note stays with the change and its archived copy is removed again"
    _refused(result, "archive_note_changed")
    assert result.details["detail"] == "kickoff.md stays in the notes folder"


def test_an_archived_note_changed_while_it_is_restored_keeps_the_change_and_no_copy_is_left(world, monkeypatch) -> None:
    archived = _note_bytes("Kickoff", "agenda: intro and goals", extra=f"archived_at: {CREATED}\n")
    (world.notes / ".archive").mkdir()
    entry = world.notes / ".archive" / "kickoff.md"
    entry.write_bytes(archived)
    fired = _stage_edits(monkeypatch, after_write=(lambda path: path.parent == world.notes,
                                                   lambda _path: _append(entry, EDIT)))

    result = _restore("Kickoff")

    assert fired == ["after_write"], "the archived note was changed right after its restored copy was written"
    assert _tree(world.workspace) == {"docs": "dir", "notes": "dir", "notes/.archive": "dir",
                                      "notes/.archive/kickoff.md": archived + EDIT}, \
        "the archived note stays with the change and its restored copy is removed again"
    _refused(result, "restore_note_changed")
    assert result.details["detail"] == "kickoff.md stays archived"


def test_a_note_and_its_archived_copy_both_changed_during_the_move_both_stay_with_their_changes(world, monkeypatch) -> None:
    original = _note_bytes("Kickoff", "agenda: intro and goals")
    note, copy = world.notes / "kickoff.md", world.notes / ".archive" / "kickoff.md"
    note.write_bytes(original)
    fired = _stage_edits(monkeypatch,
                         after_write=(lambda path: path == copy, lambda _path: _append(note, EDIT)),
                         before_remove=(lambda path: path == copy, lambda path: _append(path, COPY_EDIT)))

    result = _archive("Kickoff")

    assert fired[:1] == ["after_write"], "the note was changed right after its archived copy was written"
    _refused(result, "archive_move_incomplete")
    assert fired == ["after_write", "before_remove"], "the archived copy was changed just before it was removed again"
    assert result.details["detail"] == "kickoff.md and its archived copy kickoff.md both remain"
    assert note.read_bytes() == original + EDIT, "the note keeps the change made to it"
    kept = copy.read_bytes()
    assert kept == _as_archived(original, _stamp_line(kept, "archived_at")) + COPY_EDIT, \
        "the archived copy keeps the change made to it"
    assert _tree(world.workspace) == {"docs": "dir", "notes": "dir", "notes/.archive": "dir",
                                      "notes/.archive/kickoff.md": kept, "notes/kickoff.md": original + EDIT}


def test_an_archived_note_and_its_restored_copy_both_changed_during_the_move_both_stay_with_their_changes(world, monkeypatch) -> None:
    archived = _note_bytes("Kickoff", "agenda: intro and goals", extra=f"archived_at: {CREATED}\n")
    (world.notes / ".archive").mkdir()
    entry = world.notes / ".archive" / "kickoff.md"
    entry.write_bytes(archived)
    fired = _stage_edits(monkeypatch,
                         after_write=(lambda path: path.parent == world.notes, lambda _path: _append(entry, EDIT)),
                         before_remove=(lambda path: path.parent == world.notes, lambda path: _append(path, COPY_EDIT)))

    result = _restore("Kickoff")

    assert fired[:1] == ["after_write"], "the archived note was changed right after its restored copy was written"
    _refused(result, "restore_move_incomplete")
    assert fired == ["after_write", "before_remove"], "the restored copy was changed just before it was removed again"
    copies = sorted(world.notes.glob("*.md"))
    assert len(copies) == 1, copies
    restored = copies[0]
    assert result.details["detail"] == f"kickoff.md and its restored copy {restored.name} both remain"
    assert entry.read_bytes() == archived + EDIT, "the archived note keeps the change made to it"
    kept = restored.read_bytes()
    assert kept == _as_restored(_note_bytes("Kickoff", "agenda: intro and goals"), _stamp_line(kept, "updated")) + COPY_EDIT, \
        "the restored copy keeps the change made to it"
    assert _tree(world.workspace) == {"docs": "dir", "notes": "dir", "notes/.archive": "dir",
                                      "notes/.archive/kickoff.md": archived + EDIT, f"notes/{restored.name}": kept}


# --- link_note_to_event on paths the calendar route never stores ---------------------------------------
# The calendar route stores the path `find_named_action` returned, which is always a resolved `.md`
# directly in the notes folder; a `.txt` there, a note one folder down or a path that climbs out of the
# notes folder can only reach link_note_to_event directly.


def test_linking_an_event_refuses_a_plain_text_file_in_the_notes_folder(world) -> None:
    from core.operator.notes import link_note_to_event

    text_file = world.notes / "todo.txt"
    text_file.write_bytes(_note_bytes("Todo", "a text file, not a note"))

    linked = link_note_to_event(str(text_file), event_uid="evt-1", action_id="act-1")

    assert text_file.read_bytes() == _note_bytes("Todo", "a text file, not a note")
    assert linked is False


@pytest.mark.parametrize("folder", ["sub", ".archive"])
def test_linking_an_event_refuses_a_note_in_a_folder_below_the_notes_folder(world, folder) -> None:
    from core.operator.notes import link_note_to_event

    nested = world.notes / folder / "plan.md"
    nested.parent.mkdir()
    nested.write_bytes(_note_bytes("Plan", "a note one folder down"))
    (world.notes / "plan.md").write_bytes(_note_bytes("Plan note", "a real note with the same file name"))
    workspace_before = _tree(world.workspace)

    linked = link_note_to_event(str(nested), event_uid="evt-4", action_id="act-4")

    assert _tree(world.workspace) == workspace_before, "neither the nested file nor the same-named note changed"
    assert linked is False


def test_linking_an_event_refuses_a_path_that_climbs_out_of_the_notes_folder(world) -> None:
    from core.operator.notes import link_note_to_event

    plan = world.docs / "plan.md"
    plan.write_bytes(_note_bytes("Plan", "release plan kept in docs"))
    same_name = world.notes / "plan.md"
    same_name.write_bytes(_note_bytes("Plan note", "a real note with the same file name"))
    workspace_before = _tree(world.workspace)

    for climbing in (str(world.notes / ".." / "docs" / "plan.md"), "notes/../docs/plan.md"):
        linked = link_note_to_event(climbing, event_uid="evt-2", action_id="act-2")

        assert _tree(world.workspace) == workspace_before, "neither the docs file nor the same-named note changed"
        assert linked is False, climbing


def test_linking_an_event_refuses_a_note_outside_the_workspace(world) -> None:
    from core.operator.notes import link_note_to_event

    stray = world.outside / "stray.md"
    stray.write_bytes(_note_bytes("Stray", "outside the project"))

    linked = link_note_to_event(str(stray), event_uid="evt-3")

    assert stray.read_bytes() == _note_bytes("Stray", "outside the project")
    assert linked is False
