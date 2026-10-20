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
* a note (or an archived note) that another editor changes, or another program replaces with another file
  or a folder, while it is moved is never reported as archived or restored: the changed note or the
  replacement stays as it is, and VOOL leaves the new copy, the note as VOOL read it
  (``archive_note_changed`` / ``restore_note_changed``);
* a note (or an archived note, or the whole archive folder) that another program moves or removes while
  it is moved is never reported as archived or restored, and VOOL leaves the new copy, since it may
  be the only one left (``archive_note_gone`` / ``restore_note_gone``); the same when the notes folder is
  replaced by a file;
* a note whose folder this process can no longer search right after the new copy is written (POSIX mode
  bits) is reported neither as gone nor as staying, and VOOL leaves the new copy too
  (``archive_note_unchecked`` / ``restore_note_unchecked``);
* a note that cannot be removed (its folder no longer writable, POSIX mode bits; on Windows, held open by
  another program) stays with the bytes VOOL read, and the new copy is removed again
  (``archive_remove_failed`` / ``restore_remove_failed``), unless the copy changed meanwhile: then both stay
  and the answer names both (``archive_move_incomplete`` / ``restore_move_incomplete``); elsewhere the move
  completes. The same holds for a note changed while VOOL removes it and saved back with the bytes VOOL read
  (archive and restore). If that note then goes (or is replaced) while VOOL removes the copy, VOOL writes the
  copy again (``archive_note_gone`` / ``restore_note_gone``, ``archive_note_changed``), and when it cannot
  (its folder is now a file, or another file holds the name, which is left as it is) it says it kept no copy
  (``archive_copy_lost`` / ``restore_copy_lost``). If the note goes while the copy cannot be removed, or after
  another program moved the copy's folder away, the answer is that the note is gone, and VOOL writes no
  second copy;
* a file in the notes folder that is not UTF-8 stops an archive with ``archive_unreadable``, nothing moved;
* ``link_note_to_event`` writes no receipt into a ``.txt`` file, a note in a subfolder or in ``.archive``,
  a path that climbs out of the notes folder, or a note outside the workspace, and writes one into a note
  exactly when the notes folder lists it (``Plan.MD`` on Windows, not elsewhere);
* a plain note still archives and restores with the same bytes.

The receipt the calendar route writes after an approved event is pinned, with a real loopback CalDAV
server, in ``tests/test_note_event_receipt_stays_in_the_note.py``, which reuses this file's notes world and
helpers.

The concurrent changes are staged by wrapping the real writers and remover in
``core.execution.artifacts``: each wrapper calls the real function and, at the chosen moment, changes,
replaces, moves or removes a file or folder, or a folder's permissions, the way another program would.

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
                 after_write: Stage | None = None, before_remove: Stage | list[Stage] | None = None,
                 after_remove: Stage | list[Stage] | None = None) -> list[str]:
    """Run the REAL writers and remover the note code calls, with another program acting at chosen moments.

    Each moment is ``(which, edit)``, or for a removal a list of them: the first time the note code
    writes (or removes) a path for which ``which(path)`` holds, ``edit(path)`` runs just before or just after
    the real call (after a removal, whether it succeeded or was refused). Both writers are wrapped (the
    committed code writes with ``pinned_atomic_write_text``, 1c26068 wrote with ``atomic_write_text``) and the
    note code looks them up at call time, so the same change lands on either. A write inside another wrapped
    write (on the path-based branch Windows takes, ``pinned_atomic_write_text`` calls ``atomic_write_text``) is
    not a moment of its own, so an edit after a write lands once the note code's own write has returned, on
    every platform. Returns the moments that fired, in order."""
    from core.execution import artifacts

    def stages(value: Stage | list[Stage] | None) -> list[Stage]:
        return [] if value is None else value if isinstance(value, list) else [value]

    moments = {"before_write": stages(before_write), "after_write": stages(after_write),
               "before_remove": stages(before_remove), "after_remove": stages(after_remove)}
    fired: list[str] = []
    used: set[tuple[str, int]] = set()
    writing: list[object] = []

    def at(moment: str, path: object) -> None:
        for index, (which, edit) in enumerate(moments[moment]):
            if (moment, index) not in used and which(Path(str(path))):
                used.add((moment, index))
                fired.append(moment)
                edit(Path(str(path)))
                return

    def wrapped_writer(real: Callable[..., object]) -> Callable[..., object]:
        def write(path, content, *args, **kwargs):
            if writing:
                return real(path, content, *args, **kwargs)
            writing.append(path)
            try:
                at("before_write", path)
                written = real(path, content, *args, **kwargs)
            finally:
                writing.pop()
            at("after_write", path)
            return written

        return write

    def wrapped_remover(real: Callable[..., object]) -> Callable[..., object]:
        def remove(path, *args, **kwargs):
            at("before_remove", path)
            try:
                return real(path, *args, **kwargs)
            finally:
                at("after_remove", path)

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


def test_a_note_changed_while_it_is_archived_keeps_the_change_and_its_archived_copy(world, monkeypatch) -> None:
    """The note's text is then in two versions: the changed note stays where it was, and the archived copy,
    the note as VOOL read it, is not removed."""
    original = _note_bytes("Kickoff", "agenda: intro and goals")
    note, copy = world.notes / "kickoff.md", world.notes / ".archive" / "kickoff.md"
    note.write_bytes(original)
    fired = _stage_edits(monkeypatch, after_write=(lambda path: path == copy, lambda _path: _append(note, EDIT)))

    result = _archive("Kickoff")

    assert fired == ["after_write"], "the note was changed right after its archived copy was written"
    kept = copy.read_bytes()
    assert kept == _as_archived(original, _stamp_line(kept, "archived_at"))
    assert _tree(world.workspace) == {"docs": "dir", "notes": "dir", "notes/.archive": "dir",
                                      "notes/.archive/kickoff.md": kept, "notes/kickoff.md": original + EDIT}, \
        "the note stays with the change and its archived copy is kept"
    _refused(result, "archive_note_changed")
    assert result.details["detail"] == ("kickoff.md changed or was replaced in the notes folder meanwhile and was left "
                                        "as it is; VOOL wrote its archived copy kickoff.md, the note as VOOL read it, and left it there")


def test_an_archived_note_changed_while_it_is_restored_keeps_the_change_and_its_restored_copy(world, monkeypatch) -> None:
    archived = _note_bytes("Kickoff", "agenda: intro and goals", extra=f"archived_at: {CREATED}\n")
    (world.notes / ".archive").mkdir()
    entry = world.notes / ".archive" / "kickoff.md"
    entry.write_bytes(archived)
    fired = _stage_edits(monkeypatch, after_write=(lambda path: path.parent == world.notes,
                                                   lambda _path: _append(entry, EDIT)))

    result = _restore("Kickoff")

    assert fired == ["after_write"], "the archived note was changed right after its restored copy was written"
    copies = sorted(world.notes.glob("*.md"))
    assert len(copies) == 1, copies
    restored = copies[0].read_bytes()
    assert restored == _as_restored(_note_bytes("Kickoff", "agenda: intro and goals"), _stamp_line(restored, "updated"))
    assert _tree(world.workspace) == {"docs": "dir", "notes": "dir", "notes/.archive": "dir",
                                      "notes/.archive/kickoff.md": archived + EDIT, f"notes/{copies[0].name}": restored}, \
        "the archived note stays with the change and its restored copy is kept"
    _refused(result, "restore_note_changed")
    assert result.details["detail"] == ("kickoff.md changed or was replaced in the archive meanwhile and was left as it "
                                        f"is; VOOL wrote its restored copy {copies[0].name}, the note as VOOL read it, and left it there")


# --- another program moves the note away, or holds it open, while it is moved ---------------------------


def test_a_note_moved_away_while_it_is_archived_is_not_reported_archived_and_its_copy_is_kept(world, monkeypatch) -> None:
    original = _note_bytes("Kickoff", "agenda: intro and goals")
    note, copy = world.notes / "kickoff.md", world.notes / ".archive" / "kickoff.md"
    note.write_bytes(original)
    renamed = world.notes / "kickoff-renamed.md"
    fired = _stage_edits(monkeypatch, after_write=(lambda path: path == copy, lambda _path: note.rename(renamed)))

    result = _archive("Kickoff")

    assert fired == ["after_write"], "the note was renamed right after its archived copy was written"
    kept = copy.read_bytes()
    assert kept == _as_archived(original, _stamp_line(kept, "archived_at"))
    assert _tree(world.workspace) == {"docs": "dir", "notes": "dir", "notes/.archive": "dir",
                                      "notes/.archive/kickoff.md": kept, "notes/kickoff-renamed.md": original}, \
        "the renamed note stays as it is, and the archived copy is kept: VOOL cannot tell a rename from a removal"
    _refused(result, "archive_note_gone")
    assert result.details["detail"] == ("nothing is at kickoff.md in the notes folder any more; "
                                        "VOOL wrote its archived copy kickoff.md and left it there")


def test_a_note_removed_while_it_is_archived_keeps_its_archived_copy_the_only_one_left(world, monkeypatch) -> None:
    original = _note_bytes("Kickoff", "agenda: intro and goals")
    note, copy = world.notes / "kickoff.md", world.notes / ".archive" / "kickoff.md"
    note.write_bytes(original)
    fired = _stage_edits(monkeypatch, after_write=(lambda path: path == copy, lambda _path: note.unlink()),
                         before_remove=(lambda path: path == copy, lambda _path: None))

    result = _archive("Kickoff")

    assert fired == ["after_write"], ("the note was removed right after its archived copy was written, and the copy "
                                      "was never offered for removal")
    kept = copy.read_bytes()
    assert kept == _as_archived(original, _stamp_line(kept, "archived_at"))
    assert _tree(world.workspace) == {"docs": "dir", "notes": "dir", "notes/.archive": "dir",
                                      "notes/.archive/kickoff.md": kept}
    _refused(result, "archive_note_gone")


def test_an_archived_note_moved_away_while_it_is_restored_is_not_reported_restored_and_its_copy_is_kept(world, monkeypatch) -> None:
    archived = _note_bytes("Kickoff", "agenda: intro and goals", extra=f"archived_at: {CREATED}\n")
    (world.notes / ".archive").mkdir()
    entry = world.notes / ".archive" / "kickoff.md"
    entry.write_bytes(archived)
    elsewhere = world.notes / ".archive" / "kickoff-kept.md"
    fired = _stage_edits(monkeypatch, after_write=(lambda path: path.parent == world.notes,
                                                   lambda _path: entry.rename(elsewhere)))

    result = _restore("Kickoff")

    assert fired == ["after_write"], "the archived note was renamed right after its restored copy was written"
    copies = sorted(world.notes.glob("*.md"))
    assert len(copies) == 1, copies
    restored = copies[0].read_bytes()
    assert restored == _as_restored(_note_bytes("Kickoff", "agenda: intro and goals"), _stamp_line(restored, "updated"))
    assert _tree(world.workspace) == {"docs": "dir", "notes": "dir", "notes/.archive": "dir",
                                      "notes/.archive/kickoff-kept.md": archived, f"notes/{copies[0].name}": restored}
    _refused(result, "restore_note_gone")
    assert result.details["detail"] == ("nothing is at kickoff.md in the archive any more; "
                                        f"VOOL wrote its restored copy {copies[0].name} and left it there")


def test_an_archive_folder_moved_away_while_a_note_is_restored_keeps_the_restored_copy(world, monkeypatch) -> None:
    """The whole ``notes/.archive`` folder is renamed by another program (a sync client, say) right after the
    restored copy is written: the archived note is no longer where it was read, so nothing claims it stays
    archived, and the restored copy is kept."""
    archived = _note_bytes("Kickoff", "agenda: intro and goals", extra=f"archived_at: {CREATED}\n")
    (world.notes / ".archive").mkdir()
    (world.notes / ".archive" / "kickoff.md").write_bytes(archived)
    fired = _stage_edits(monkeypatch, after_write=(lambda path: path.parent == world.notes,
                                                   lambda _path: (world.notes / ".archive").rename(world.notes / "old-archive")))

    result = _restore("Kickoff")

    assert fired == ["after_write"], "the archive folder was renamed right after the restored copy was written"
    copies = sorted(world.notes.glob("*.md"))
    assert len(copies) == 1, copies
    assert _tree(world.workspace) == {"docs": "dir", "notes": "dir", "notes/old-archive": "dir",
                                      "notes/old-archive/kickoff.md": archived,
                                      f"notes/{copies[0].name}": copies[0].read_bytes()}
    _refused(result, "restore_note_gone")


def test_a_note_another_program_holds_open_stays_in_place_on_windows_and_archives_elsewhere(world) -> None:
    """Windows does not remove a file another program holds open without delete sharing (Python's own
    `open` shares only reading and writing); POSIX removes the name and the open file lives on."""
    original = _note_bytes("Kickoff", "agenda: intro and goals")
    note, copy = world.notes / "kickoff.md", world.notes / ".archive" / "kickoff.md"
    note.write_bytes(original)

    with note.open("rb"):
        result = _archive("Kickoff")

    if os.name == "nt":
        assert _tree(world.workspace) == {"docs": "dir", "notes": "dir", "notes/.archive": "dir",
                                          "notes/kickoff.md": original}, "the note stays and its copy is removed again"
        _refused(result, "archive_remove_failed")
        assert result.details["detail"] == "kickoff.md stays in the notes folder"
    else:
        assert result.ok and result.status == "executed", result.response_text
        assert not os.path.lexists(note)
        stamped = copy.read_bytes()
        assert stamped == _as_archived(original, _stamp_line(stamped, "archived_at"))


def test_an_archived_note_another_program_holds_open_stays_archived_on_windows_and_restores_elsewhere(world) -> None:
    archived = _note_bytes("Kickoff", "agenda: intro and goals", extra=f"archived_at: {CREATED}\n")
    (world.notes / ".archive").mkdir()
    entry = world.notes / ".archive" / "kickoff.md"
    entry.write_bytes(archived)

    with entry.open("rb"):
        result = _restore("Kickoff")

    if os.name == "nt":
        assert _tree(world.workspace) == {"docs": "dir", "notes": "dir", "notes/.archive": "dir",
                                          "notes/.archive/kickoff.md": archived}, \
            "the archived note stays and its restored copy is removed again"
        _refused(result, "restore_remove_failed")
        assert result.details["detail"] == "kickoff.md stays archived"
    else:
        assert result.ok and result.status == "executed", result.response_text
        assert not os.path.lexists(entry)
        assert len(list(world.notes.glob("*.md"))) == 1


def _replace_with(kind: str) -> Callable[[Path], None]:
    """Another program removes the entry at a path and puts another file, or a folder, under its name."""
    def replace(path: Path) -> None:
        path.unlink()
        if kind == "a folder":
            path.mkdir()
        else:
            path.write_bytes(b"another file under the same name\n")

    return replace


@pytest.mark.parametrize("kind", ["another file", "a folder"])
def test_a_note_replaced_while_it_is_archived_leaves_the_replacement_and_keeps_the_archived_copy(world, monkeypatch, kind) -> None:
    """The note's text is then only in its archived copy. A new file under the same name can even reuse the old
    file's identity (the same inode on Linux), so VOOL removes its copy only while the note it read is there
    with the bytes it read."""
    original = _note_bytes("Kickoff", "agenda: intro and goals")
    note, copy = world.notes / "kickoff.md", world.notes / ".archive" / "kickoff.md"
    note.write_bytes(original)
    replace = _replace_with(kind)
    fired = _stage_edits(monkeypatch, after_write=(lambda path: path == copy, lambda _path: replace(note)))

    result = _archive("Kickoff")

    assert fired == ["after_write"], f"the note was replaced by {kind} right after its archived copy was written"
    kept = copy.read_bytes()
    assert kept == _as_archived(original, _stamp_line(kept, "archived_at"))
    replacement = "dir" if kind == "a folder" else b"another file under the same name\n"
    assert _tree(world.workspace) == {"docs": "dir", "notes": "dir", "notes/.archive": "dir",
                                      "notes/.archive/kickoff.md": kept, "notes/kickoff.md": replacement}
    _refused(result, "archive_note_changed")


def test_a_notes_folder_replaced_by_a_file_while_a_note_is_archived_keeps_the_archived_copy(world, monkeypatch) -> None:
    """Another program moves the whole notes folder away and leaves a plain file named ``notes`` in its place
    right after the archived copy is written: nothing is at the note's path any more (its folder is a file)."""
    original = _note_bytes("Kickoff", "agenda: intro and goals")
    note, copy = world.notes / "kickoff.md", world.notes / ".archive" / "kickoff.md"
    note.write_bytes(original)
    moved = world.workspace / "notes-moved"

    def swap(_path: Path) -> None:
        world.notes.rename(moved)
        world.notes.write_bytes(b"not a folder\n")

    fired = _stage_edits(monkeypatch, after_write=(lambda path: path == copy, swap))

    result = _archive("Kickoff")

    assert fired == ["after_write"], "the notes folder was replaced by a file right after the archived copy was written"
    kept = (moved / ".archive" / "kickoff.md").read_bytes()
    assert kept == _as_archived(original, _stamp_line(kept, "archived_at"))
    assert _tree(world.workspace) == {"docs": "dir", "notes": b"not a folder\n", "notes-moved": "dir",
                                      "notes-moved/.archive": "dir", "notes-moved/.archive/kickoff.md": kept,
                                      "notes-moved/kickoff.md": original}
    _refused(result, "archive_note_gone")


def _deny_search(folder: Path, entry: Path) -> Callable[[Path], None]:
    """Another program takes away ``folder``'s search permission (mode 0600); the returned edit records in
    ``denied`` whether that stops this process from looking at ``entry``. It does on POSIX for a process that
    is not root; Windows does not use these mode bits for folders, and root is not bound by them."""
    def edit(_path: Path) -> None:
        folder.chmod(0o600)
        try:
            os.lstat(entry)
        except PermissionError:
            edit.denied = True

    edit.denied = False
    return edit


def test_a_note_whose_folder_cannot_be_searched_after_it_is_copied_is_not_reported_gone_and_its_copy_stays(world, monkeypatch) -> None:
    """Whether the note is still there cannot be checked, so the answer claims neither that it is gone nor
    that it stays, and the archived copy, which may be the only one left, is not removed. Where the mode
    change does not stop this process, the archive simply completes."""
    original = _note_bytes("Kickoff", "agenda: intro and goals")
    note, copy = world.notes / "kickoff.md", world.notes / ".archive" / "kickoff.md"
    note.write_bytes(original)
    lock = _deny_search(world.notes, note)
    fired = _stage_edits(monkeypatch, after_write=(lambda path: path == copy, lock))

    try:
        result = _archive("Kickoff")
    finally:
        world.notes.chmod(0o700)

    assert fired == ["after_write"], "the notes folder lost its search permission right after the copy was written"
    kept = copy.read_bytes()
    assert kept == _as_archived(original, _stamp_line(kept, "archived_at"))
    if lock.denied:
        assert _tree(world.workspace) == {"docs": "dir", "notes": "dir", "notes/.archive": "dir",
                                          "notes/.archive/kickoff.md": kept, "notes/kickoff.md": original}
        _refused(result, "archive_note_unchecked")
        assert result.details["detail"] == ("VOOL could not check whether kickoff.md is still in the notes folder "
                                            "after writing its archived copy kickoff.md; it left that copy there")
    else:
        assert result.ok and result.status == "executed", result.response_text
        assert not os.path.lexists(note)


def test_an_archived_note_whose_archive_cannot_be_searched_after_it_is_copied_is_not_reported_gone_and_its_copy_stays(world, monkeypatch) -> None:
    archived = _note_bytes("Kickoff", "agenda: intro and goals", extra=f"archived_at: {CREATED}\n")
    archive = world.notes / ".archive"
    archive.mkdir()
    entry = archive / "kickoff.md"
    entry.write_bytes(archived)
    lock = _deny_search(archive, entry)
    fired = _stage_edits(monkeypatch, after_write=(lambda path: path.parent == world.notes, lock))

    try:
        result = _restore("Kickoff")
    finally:
        archive.chmod(0o700)

    assert fired == ["after_write"], "the archive folder lost its search permission right after the copy was written"
    copies = sorted(world.notes.glob("*.md"))
    assert len(copies) == 1, copies
    restored = copies[0].read_bytes()
    assert restored == _as_restored(_note_bytes("Kickoff", "agenda: intro and goals"), _stamp_line(restored, "updated"))
    if lock.denied:
        assert _tree(world.workspace) == {"docs": "dir", "notes": "dir", "notes/.archive": "dir",
                                          "notes/.archive/kickoff.md": archived, f"notes/{copies[0].name}": restored}
        _refused(result, "restore_note_unchecked")
        assert result.details["detail"] == ("VOOL could not check whether kickoff.md is still in the archive after "
                                            f"writing its restored copy {copies[0].name}; it left that copy there")
    else:
        assert result.ok and result.status == "executed", result.response_text
        assert not os.path.lexists(entry)


def _deny_writes(folder: Path) -> Callable[[Path], None]:
    """Another program takes away ``folder``'s write permission (mode 0500); the returned edit records in
    ``denied`` whether that stops this process from adding or removing entries there. It does on POSIX for a
    process that is not root; Windows does not use these mode bits for folders, and root is not bound by them."""
    def edit(_path: Path) -> None:
        folder.chmod(0o500)
        probe = folder / ".write-probe"
        try:
            probe.touch(exist_ok=False)
        except PermissionError:
            edit.denied = True
        else:
            probe.unlink()

    edit.denied = False
    return edit


def _remove_archive_folder(notes: Path) -> Callable[[Path], None]:
    """Another program removes the whole ``.archive`` folder, copy and all (giving the notes folder its write
    permission back to do so)."""
    def edit(_path: Path) -> None:
        import shutil

        notes.chmod(0o700)
        shutil.rmtree(notes / ".archive")

    return edit


@pytest.mark.parametrize("then", ["stays", "is removed once the copy is gone"])
def test_a_note_changed_and_saved_back_while_it_is_removed_stays_and_its_copy_goes_unless_the_note_goes(world, monkeypatch, then) -> None:
    """Another editor changes the note while VOOL removes it, so the removal is refused, then saves back the
    bytes VOOL read: the note is there as read, so the archived copy is removed again and the answer says the
    note stays. If the note is then removed right after the copy, VOOL writes the copy again, as it is the only
    place the note's text is left."""
    original = _note_bytes("Kickoff", "agenda: intro and goals")
    note, copy = world.notes / "kickoff.md", world.notes / ".archive" / "kickoff.md"
    note.write_bytes(original)
    written: list[bytes] = []
    after_remove = [(lambda path: path == note, lambda path: path.write_bytes(original))]
    if then != "stays":
        after_remove.append((lambda path: path == copy, lambda _path: note.unlink()))
    fired = _stage_edits(monkeypatch,
                         after_write=(lambda path: path == copy, lambda path: written.append(path.read_bytes())),
                         before_remove=(lambda path: path == note, lambda path: _append(path, EDIT)),
                         after_remove=after_remove)

    result = _archive("Kickoff")

    if then == "stays":
        assert fired == ["after_write", "before_remove", "after_remove"]
        assert _tree(world.workspace) == {"docs": "dir", "notes": "dir", "notes/.archive": "dir",
                                          "notes/kickoff.md": original}
        _refused(result, "archive_remove_failed")
        assert result.details["detail"] == "kickoff.md stays in the notes folder"
        return
    assert fired == ["after_write", "before_remove", "after_remove", "after_remove"]
    assert _tree(world.workspace) == {"docs": "dir", "notes": "dir", "notes/.archive": "dir",
                                      "notes/.archive/kickoff.md": written[0]}, \
        "the note is gone, and its archived copy is written again with the same bytes"
    assert written[0] == _as_archived(original, _stamp_line(written[0], "archived_at"))
    _refused(result, "archive_note_gone")
    assert result.details["detail"] == ("nothing is at kickoff.md in the notes folder any more; "
                                        "VOOL wrote its archived copy kickoff.md and left it there")


STRANGER = b"another program's file\n"


@pytest.mark.parametrize("how", ["the note is replaced", "the note goes and the copy cannot be written back",
                                 "the note goes and another file takes the copy's name",
                                 "the copy cannot be removed and the note stays", "the copy cannot be removed and the note goes",
                                 "the copy's folder is moved away and the note goes"])
def test_a_note_saved_back_whose_archived_copy_is_then_removed_is_answered_as_it_ends(world, monkeypatch, how) -> None:
    """As above, the note is changed while VOOL removes it and saved back with the bytes VOOL read, so VOOL
    removes its archived copy again; then another program acts. When VOOL removed the copy and the note was
    then replaced, VOOL writes the copy again. When it cannot write it back (the emptied archive folder is now
    a file, or another file took the copy's name, which VOOL leaves as it is), the answer says that VOOL kept
    no copy. When VOOL could not remove the copy (it changed meanwhile), both stay, and the answer names both
    while the note stays, or says the note is gone when it went; when another program moved the copy's folder
    away first, the copy is where that left it. In neither case does VOOL write a second copy."""
    original = _note_bytes("Kickoff", "agenda: intro and goals")
    note, archive = world.notes / "kickoff.md", world.notes / ".archive"
    copy = archive / "kickoff.md"
    note.write_bytes(original)
    written: list[bytes] = []

    def then(path: Path) -> None:
        if how == "the note is replaced":
            _replace_with("another file")(note)
            return
        if how != "the copy cannot be removed and the note stays":
            note.unlink()
        if how == "the note goes and the copy cannot be written back":
            archive.rmdir()
            archive.write_bytes(STRANGER)
        elif how == "the note goes and another file takes the copy's name":
            path.write_bytes(STRANGER)
        elif how.startswith("the copy cannot be removed"):
            _append(path, COPY_EDIT)
        elif how.startswith("the copy's folder is moved away"):
            archive.rename(world.notes / "old-archive")

    after_copy = how.startswith("the note")
    fired = _stage_edits(monkeypatch,
                         after_write=(lambda path: path == copy, lambda path: written.append(path.read_bytes())),
                         before_remove=[(lambda path: path == note, lambda path: _append(path, EDIT)),
                                        (lambda path: path == copy and not after_copy, then)],
                         after_remove=[(lambda path: path == note, lambda path: path.write_bytes(original)),
                                       (lambda path: path == copy and after_copy, then)])

    result = _archive("Kickoff")

    assert written[0] == _as_archived(original, _stamp_line(written[0], "archived_at"))
    assert fired == ["after_write", "before_remove", "after_remove", "after_remove" if after_copy else "before_remove"]
    base = {"docs": "dir", "notes": "dir"}
    if how == "the note is replaced":
        assert _tree(world.workspace) == {**base, "notes/.archive": "dir", "notes/.archive/kickoff.md": written[0],
                                          "notes/kickoff.md": b"another file under the same name\n"}, \
            "the replacement stays as it is, and the archived copy is written again with the same bytes"
        _refused(result, "archive_note_changed")
        return
    if after_copy:
        archived = ({"notes/.archive": STRANGER} if how == "the note goes and the copy cannot be written back"
                    else {"notes/.archive": "dir", "notes/.archive/kickoff.md": STRANGER})
        assert _tree(world.workspace) == {**base, **archived}, \
            "the note is gone, VOOL removed its copy, and another program's file is left as it is"
        _refused(result, "archive_copy_lost")
        assert result.details["detail"] == ("VOOL removed its archived copy kickoff.md again while kickoff.md was still "
                                            "in the notes folder, then could not confirm that kickoff.md is still there "
                                            "as VOOL read it, and could not write the copy back, so it kept no copy of "
                                            "the note")
        return
    if how == "the copy cannot be removed and the note stays":
        assert _tree(world.workspace) == {**base, "notes/.archive": "dir", "notes/.archive/kickoff.md": written[0] + COPY_EDIT,
                                          "notes/kickoff.md": original}
        _refused(result, "archive_move_incomplete")
        assert result.details["detail"] == ("kickoff.md stays in the notes folder, and VOOL could not remove its "
                                            "archived copy kickoff.md")
        return
    left = ({"notes/.archive": "dir", "notes/.archive/kickoff.md": written[0] + COPY_EDIT}
            if how == "the copy cannot be removed and the note goes"
            else {"notes/old-archive": "dir", "notes/old-archive/kickoff.md": written[0]})
    assert _tree(world.workspace) == {**base, **left}, \
        "the copy VOOL did not remove is where it was left, and no second copy is written"
    _refused(result, "archive_note_gone")
    assert result.details["detail"] == ("nothing is at kickoff.md in the notes folder any more; "
                                        "VOOL wrote its archived copy kickoff.md and left it there")


@pytest.mark.parametrize("then", ["stays", "is removed once the copy is gone", "is removed and another file takes the copy's name"])
def test_an_archived_note_changed_and_saved_back_while_it_is_restored_stays_and_its_copy_goes_unless_it_goes(world, monkeypatch, then) -> None:
    """Restore's side of the two tests above: the archived note is changed while VOOL removes it and saved
    back with the bytes VOOL read, so the restored copy is removed again; if the archived note is then removed,
    VOOL writes the restored copy again, or, when another file has taken its name, leaves that file as it is
    and says it kept no copy."""
    archived = _note_bytes("Kickoff", "agenda: intro and goals", extra=f"archived_at: {CREATED}\n")
    (world.notes / ".archive").mkdir()
    entry = world.notes / ".archive" / "kickoff.md"
    entry.write_bytes(archived)
    written: list[tuple[Path, bytes]] = []

    def gone(path: Path) -> None:
        entry.unlink()
        if then != "is removed once the copy is gone":
            path.write_bytes(STRANGER)

    after_remove = [(lambda path: path == entry, lambda path: path.write_bytes(archived))]
    if then != "stays":
        after_remove.append((lambda path: path.parent == world.notes, gone))
    fired = _stage_edits(monkeypatch,
                         after_write=(lambda path: path.parent == world.notes,
                                      lambda path: written.append((path, path.read_bytes()))),
                         before_remove=(lambda path: path == entry, lambda path: _append(path, EDIT)),
                         after_remove=after_remove)

    result = _restore("Kickoff")

    [(restored, restored_bytes)] = written
    assert restored_bytes == _as_restored(_note_bytes("Kickoff", "agenda: intro and goals"),
                                          _stamp_line(restored_bytes, "updated"))
    if then == "stays":
        assert fired == ["after_write", "before_remove", "after_remove"]
        assert _tree(world.workspace) == {"docs": "dir", "notes": "dir", "notes/.archive": "dir",
                                          "notes/.archive/kickoff.md": archived}
        _refused(result, "restore_remove_failed")
        assert result.details["detail"] == "kickoff.md stays archived"
        return
    assert fired == ["after_write", "before_remove", "after_remove", "after_remove"]
    if then == "is removed once the copy is gone":
        assert _tree(world.workspace) == {"docs": "dir", "notes": "dir", "notes/.archive": "dir",
                                          f"notes/{restored.name}": restored_bytes}, \
            "the archived note is gone, and its restored copy is written again with the same bytes"
        _refused(result, "restore_note_gone")
        assert result.details["detail"] == ("nothing is at kickoff.md in the archive any more; "
                                            f"VOOL wrote its restored copy {restored.name} and left it there")
        return
    assert _tree(world.workspace) == {"docs": "dir", "notes": "dir", "notes/.archive": "dir",
                                      f"notes/{restored.name}": STRANGER}, \
        "the archived note is gone, and another program's file under the copy's name is left as it is"
    _refused(result, "restore_copy_lost")
    assert result.details["detail"] == (f"VOOL removed its restored copy {restored.name} again while kickoff.md was "
                                        "still in the archive, then could not confirm that kickoff.md is still there "
                                        "as VOOL read it, and could not write the copy back, so it kept no copy of the "
                                        "note")


@pytest.mark.parametrize("copy_then", ["left alone", "changed", "removed with its folder"])
def test_a_note_that_cannot_be_removed_stays_and_its_archived_copy_goes_unless_it_changed(world, monkeypatch, copy_then) -> None:
    """Right after the archived copy is written another program takes away the notes folder's write permission,
    so the note cannot be removed. The note is still there with the bytes VOOL read, so the copy is removed
    again: the answer says the note stays, and names the copy only when it changed meanwhile and so was kept
    (and never when it is gone). Where the mode change does not stop this process, the archive completes and
    the copy is never offered for removal."""
    original = _note_bytes("Kickoff", "agenda: intro and goals")
    note, copy = world.notes / "kickoff.md", world.notes / ".archive" / "kickoff.md"
    note.write_bytes(original)
    lock = _deny_writes(world.notes)
    then = {"left alone": None, "changed": lambda path: _append(path, COPY_EDIT),
            "removed with its folder": _remove_archive_folder(world.notes)}[copy_then]
    fired = _stage_edits(monkeypatch, after_write=(lambda path: path == copy, lock),
                         before_remove=(lambda path: path == copy and then is not None, lambda path: then(path)))

    try:
        result = _archive("Kickoff")
    finally:
        world.notes.chmod(0o700)

    if not lock.denied:
        assert fired == ["after_write"]
        assert result.ok and result.status == "executed", result.response_text
        assert not os.path.lexists(note)
        return
    assert fired == (["after_write"] if then is None else ["after_write", "before_remove"])
    assert note.read_bytes() == original, "the note stays with its bytes"
    if copy_then == "changed":
        kept = copy.read_bytes()
        assert kept == _as_archived(original, _stamp_line(kept, "archived_at")) + COPY_EDIT
        assert _tree(world.workspace) == {"docs": "dir", "notes": "dir", "notes/.archive": "dir",
                                          "notes/.archive/kickoff.md": kept, "notes/kickoff.md": original}
        _refused(result, "archive_move_incomplete")
        assert result.details["detail"] == ("kickoff.md stays in the notes folder, and VOOL could not remove its "
                                            "archived copy kickoff.md")
        return
    archive = {"notes/.archive": "dir"} if copy_then == "left alone" else {}
    assert _tree(world.workspace) == {"docs": "dir", "notes": "dir", **archive, "notes/kickoff.md": original}
    _refused(result, "archive_remove_failed")
    assert result.details["detail"] == "kickoff.md stays in the notes folder"


@pytest.mark.parametrize("copy_then", ["left alone", "changed"])
def test_an_archived_note_that_cannot_be_removed_stays_archived_and_its_restored_copy_goes_unless_it_changed(world, monkeypatch, copy_then) -> None:
    """Restore's side of the test above: the archive folder loses its write permission right after the
    restored copy is written."""
    archived = _note_bytes("Kickoff", "agenda: intro and goals", extra=f"archived_at: {CREATED}\n")
    archive = world.notes / ".archive"
    archive.mkdir()
    entry = archive / "kickoff.md"
    entry.write_bytes(archived)
    lock = _deny_writes(archive)
    changed = copy_then == "changed"
    fired = _stage_edits(monkeypatch, after_write=(lambda path: path.parent == world.notes, lock),
                         before_remove=(lambda path: changed and path.parent == world.notes,
                                        lambda path: _append(path, COPY_EDIT)))

    try:
        result = _restore("Kickoff")
    finally:
        archive.chmod(0o700)

    copies = sorted(world.notes.glob("*.md"))
    if not lock.denied:
        assert fired == ["after_write"]
        assert result.ok and result.status == "executed", result.response_text
        assert not os.path.lexists(entry) and len(copies) == 1
        return
    assert fired == (["after_write", "before_remove"] if changed else ["after_write"])
    assert entry.read_bytes() == archived, "the archived note stays with its bytes"
    if changed:
        assert len(copies) == 1, copies
        kept = copies[0].read_bytes()
        assert kept == _as_restored(_note_bytes("Kickoff", "agenda: intro and goals"), _stamp_line(kept, "updated")) + COPY_EDIT
        assert _tree(world.workspace) == {"docs": "dir", "notes": "dir", "notes/.archive": "dir",
                                          "notes/.archive/kickoff.md": archived, f"notes/{copies[0].name}": kept}
        _refused(result, "restore_move_incomplete")
        assert result.details["detail"] == (f"kickoff.md stays archived, and VOOL could not remove its restored copy "
                                            f"{copies[0].name}")
        return
    assert _tree(world.workspace) == {"docs": "dir", "notes": "dir", "notes/.archive": "dir",
                                      "notes/.archive/kickoff.md": archived}
    _refused(result, "restore_remove_failed")
    assert result.details["detail"] == "kickoff.md stays archived"


def test_a_file_in_the_notes_folder_that_is_not_utf8_stops_an_archive_and_says_why(world) -> None:
    (world.notes / "kickoff.md").write_bytes(_note_bytes("Kickoff", "agenda: intro and goals"))
    (world.notes / "legacy.md").write_bytes("---\ntitle: Legacy\n---\n\ncaf\u00e9 from an old editor\n".encode("latin-1"))
    workspace_before = _tree(world.workspace)

    result = _archive("Kickoff")

    assert _tree(world.workspace) == workspace_before, "nothing moves"
    _refused(result, "archive_unreadable")
    assert result.details["detail"].startswith("a file in the notes folder is not UTF-8 text"), result.details


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


def test_linking_an_event_writes_into_a_note_exactly_when_the_notes_folder_lists_it(world) -> None:
    """A note named with an upper-case extension is listed (read, searched, offered for an event) on
    Windows, where file names ignore case, and not elsewhere; its receipt follows the same rule."""
    from core.operator.notes import link_note_to_event, search_notes

    original = _note_bytes("Plan", "[action] call the venue")
    upper = world.notes / "Plan.MD"
    upper.write_bytes(original)

    listed = [Path(row["note_path"]).name for row in search_notes(query="")]
    linked = link_note_to_event(str(upper), event_uid="evt-5", action_id="act-5")

    assert listed == (["Plan.MD"] if os.name == "nt" else []), listed
    assert linked is (os.name == "nt")
    written = upper.read_bytes()
    if linked:
        assert written == _as_restored(original, _stamp_line(written, "updated")).replace(
            b"session: notes-owner\n", b"session: notes-owner\nlinked_event_uid: evt-5\nlinked_action_id: act-5\n")
    else:
        assert written == original


def test_linking_an_event_refuses_a_note_outside_the_workspace(world) -> None:
    from core.operator.notes import link_note_to_event

    stray = world.outside / "stray.md"
    stray.write_bytes(_note_bytes("Stray", "outside the project"))

    linked = link_note_to_event(str(stray), event_uid="evt-3")

    assert stray.read_bytes() == _note_bytes("Stray", "outside the project")
    assert linked is False
