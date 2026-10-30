"""A staged attachment swapped for a link BETWEEN its check and its open is still refused.

#106 refuses a staged attachment on Windows when ``path.is_symlink()`` is true, and only then
opens it. Windows defines no ``O_NOFOLLOW``, so nothing pins the entry across those two steps:
another process can swap the staged file for a link after the check and before the open, and the
open then follows the link without a word, handing back the TARGET's bytes as though they were
the attachment's. The open is therefore verified after the fact: what the descriptor describes
(``os.fstat``) must still be what the NAME describes (``os.lstat`` -- the entry itself, never a
link's target), the entry must be a plain file rather than a link or any other reparse point, and
a name that can no longer be stat'ed is an unknown state, which is refused with the fd closed.
POSIX keeps the ``O_NOFOLLOW``/``ELOOP`` verdicts it has always had, byte for byte.

The races are planted by wrapping ``os.open`` inside the exact window the check-then-open gap
leaves open, and every wrapper still calls the real ``os.open`` -- no reader behaviour is stubbed
out. Both doors a user actually reaches are pinned (previewing a staged attachment, and sending
one with a message), and an honest staged file is read through the same two doors in the same
run, so a refusal this broad cannot hide behind tests that only ever plant swaps.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from core import runtime_paths

pytestmark = pytest.mark.usefixtures("_isolated_home")


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("VOOL_HOME", str(tmp_path))
    runtime_paths.configure_runtime_home(tmp_path)
    yield
    runtime_paths.configure_runtime_home(None)


SESSION = "openclaw:aaaaaaaaaaaaaaaaaaaa"


def _authority():
    from core import chat_attachments

    return chat_attachments


def _stage(a, *, data: bytes = b"real staged bytes") -> dict[str, Any]:
    return a.stage_attachment(
        session_id=SESSION, declared_name="notes.txt", declared_type="text/plain", data=data
    )


def _victim_path(a, record: dict[str, Any]) -> Path:
    return a.stage_dir() / (record["id"] + ".bin")


def _outside_file(name: str, data: bytes) -> Path:
    """A file outside the staging area, holding bytes nothing staged ever held."""
    outside = Path(runtime_paths.active_vool_home()) / name
    outside.write_bytes(data)
    return outside


def _swap_in_a_link_before_open(monkeypatch, victim: Path, secret: Path) -> None:
    """Make the entry at ``victim`` a link to ``secret`` just before ``os.open`` reaches it.

    This is the #106 window itself: the module has already run (and passed) its ``is_symlink``
    pre-check by then, and Windows has no ``O_NOFOLLOW`` to catch the swap at the open. Every
    other path is opened untouched.
    """
    real_open = os.open

    def swapped_open(path, *args, **kwargs):
        if Path(path) == victim:
            victim.unlink()
            os.symlink(secret, victim)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", swapped_open)


def _hand_back_a_descriptor_for_a_different_file(monkeypatch, victim: Path, replacement: Path) -> None:
    """Leave the runtime holding a descriptor for one file while the name describes another.

    That disagreement is what the post-open identity check exists to catch. On Windows it cannot
    be staged by renaming over the entry afterwards -- the descriptor the runtime itself holds
    denies delete-sharing, so the name is pinned for as long as it is open -- so the race is
    planted at the same ``os.open`` boundary the link swap above uses: the wrapper opens the
    replacement and hands back ITS descriptor while the name keeps describing the staged file.
    Every other path is opened untouched.
    """
    real_open = os.open

    def swapped_open(path, *args, **kwargs):
        if Path(path) == victim:
            return real_open(replacement, *args, **kwargs)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", swapped_open)


def test_a_staged_attachment_swapped_for_a_link_between_its_check_and_its_open_is_refused_at_the_preview_door(
    monkeypatch,
) -> None:
    a = _authority()
    record = _stage(a)
    secret = _outside_file("secret.txt", b"TOP SECRET")
    _swap_in_a_link_before_open(monkeypatch, _victim_path(a, record), secret)
    with pytest.raises(a.AttachmentRefused) as info:
        a.read_staged_bytes(session_id=SESSION, attachment_id=record["id"])
    assert info.value.code == "symlink_refused"
    # The link's target is untouched, and it was never handed back as the attachment's bytes --
    # which is exactly what the unguarded open did on Windows.
    assert secret.read_bytes() == b"TOP SECRET"


def test_a_staged_attachment_swapped_for_a_link_between_its_check_and_its_open_is_refused_at_the_send_door(
    monkeypatch,
) -> None:
    a = _authority()
    record = _stage(a)
    secret = _outside_file("secret.txt", b"TOP SECRET")
    a.bind_to_turn(session_id=SESSION, turn_id="turn-1", attachment_ids=[record["id"]])
    _swap_in_a_link_before_open(monkeypatch, _victim_path(a, record), secret)
    with pytest.raises(a.AttachmentRefused) as info:
        a.model_attachments_for_turn(session_id=SESSION, turn_id="turn-1")
    assert info.value.code == "symlink_refused"
    assert secret.read_bytes() == b"TOP SECRET"


def test_a_staged_attachment_whose_descriptor_is_not_the_named_file_is_refused_on_windows(
    monkeypatch,
) -> None:
    """On Windows the open is verified against the name it was opened by. POSIX keeps its
    byte-for-byte behaviour -- the read answers with the descriptor's own bytes and never grows a
    new refusal -- which is what pins the Windows-only branch to Windows."""
    a = _authority()
    record = _stage(a)
    replacement = _outside_file("replacement.txt", b"OTHER FILE")
    _hand_back_a_descriptor_for_a_different_file(monkeypatch, _victim_path(a, record), replacement)
    if os.name == "nt":
        with pytest.raises(a.AttachmentRefused) as info:
            a.read_staged_bytes(session_id=SESSION, attachment_id=record["id"])
        assert info.value.code == "symlink_refused"
        return
    preview = a.read_staged_bytes(session_id=SESSION, attachment_id=record["id"])
    assert preview is not None and preview[1] == b"OTHER FILE"


def test_a_staged_attachment_that_cannot_be_verified_after_the_open_is_refused_on_windows(
    monkeypatch,
) -> None:
    """Unknown fails closed: an entry that cannot be stat'ed after it was opened is refused rather
    than trusted. POSIX never re-stats the name, so its read is unaffected."""
    a = _authority()
    record = _stage(a)
    victim = _victim_path(a, record)
    real_lstat = os.lstat

    def vanishing_lstat(path, *args, **kwargs):
        if Path(path) == victim:
            raise FileNotFoundError(f"the entry vanished: {path}")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", vanishing_lstat)
    if os.name == "nt":
        with pytest.raises(a.AttachmentRefused) as info:
            a.read_staged_bytes(session_id=SESSION, attachment_id=record["id"])
        assert info.value.code == "symlink_refused"
        return
    preview = a.read_staged_bytes(session_id=SESSION, attachment_id=record["id"])
    assert preview is not None and preview[1] == b"real staged bytes"


def test_an_untouched_staged_attachment_still_reads_through_both_doors() -> None:
    a = _authority()
    record = _stage(a)
    preview = a.read_staged_bytes(session_id=SESSION, attachment_id=record["id"])
    assert preview is not None and preview[0]["id"] == record["id"]
    assert preview[1] == b"real staged bytes"
    a.bind_to_turn(session_id=SESSION, turn_id="turn-1", attachment_ids=[record["id"]])
    sent = a.model_attachments_for_turn(session_id=SESSION, turn_id="turn-1")
    assert [entry["text"] for entry in sent] == ["real staged bytes"]
