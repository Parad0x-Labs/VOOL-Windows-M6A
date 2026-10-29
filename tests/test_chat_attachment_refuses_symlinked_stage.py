"""A staged attachment replaced by a symbolic link is refused BEFORE it is opened.

`_open_regular` guards every read of staged bytes with ``O_NOFOLLOW`` and translates ``ELOOP``
into a typed refusal -- a guard that only exists where the flag does. Windows defines no
``O_NOFOLLOW``, so on that platform the old code fell straight through to ``os.open``, which
follows a planted link silently and hands back the TARGET's bytes: an attachment swapped for a
link after staging would have been read as whatever the link points at. The refusal now happens
before the open, on the platform check the module already uses, and POSIX keeps the
``O_NOFOLLOW``/``ELOOP`` path it always had -- so these tests pass unchanged on both platforms.

The two doors a user actually reaches are pinned separately: previewing a staged attachment
(`read_staged_bytes`) and sending one with a message (`bind_to_turn` then
`model_attachments_for_turn`). An honest staged file is read through the same two doors in the
same run, so a refusal this broad cannot hide behind a test that only ever plants links.
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


def _stage_replaced_by_a_link(a, *, data: bytes = b"real staged bytes") -> tuple[Path, dict[str, Any]]:
    """Stage a text attachment, then swap its stored bytes for a link to an outside file.

    Returns ``(secret, record)``: the file the link points at (outside the staging area, holding
    bytes nothing in the staging area ever held) and the staged attachment's public record.
    """
    record = a.stage_attachment(session_id=SESSION, declared_name="notes.txt", declared_type="text/plain", data=data)
    secret = Path(runtime_paths.active_vool_home()) / "secret.txt"
    secret.write_bytes(b"TOP SECRET")
    victim = a.stage_dir() / (record["id"] + ".bin")
    victim.unlink()
    os.symlink(secret, victim)
    return secret, record


def test_a_staged_attachment_replaced_by_a_symlink_is_refused_at_the_preview_door() -> None:
    a = _authority()
    secret, record = _stage_replaced_by_a_link(a)
    with pytest.raises(a.AttachmentRefused) as info:
        a.read_staged_bytes(session_id=SESSION, attachment_id=record["id"])
    assert info.value.code == "symlink_refused"
    # Refusal is not deletion: the link's target is untouched, and it was never read as the
    # attachment's bytes (on Windows the old code returned exactly these bytes here).
    assert secret.read_bytes() == b"TOP SECRET"


def test_a_staged_attachment_replaced_by_a_symlink_is_refused_at_the_send_door() -> None:
    a = _authority()
    secret, record = _stage_replaced_by_a_link(a)
    bound = a.bind_to_turn(session_id=SESSION, turn_id="turn-1", attachment_ids=[record["id"]])
    assert bound[0]["state"] == "bound"
    with pytest.raises(a.AttachmentRefused) as info:
        a.model_attachments_for_turn(session_id=SESSION, turn_id="turn-1")
    assert info.value.code == "symlink_refused"
    assert secret.read_bytes() == b"TOP SECRET"


def test_an_untouched_staged_attachment_still_reads_through_both_doors() -> None:
    a = _authority()
    record = a.stage_attachment(
        session_id=SESSION, declared_name="notes.txt", declared_type="text/plain", data=b"real staged bytes"
    )
    preview = a.read_staged_bytes(session_id=SESSION, attachment_id=record["id"])
    assert preview is not None and preview[0]["id"] == record["id"]
    assert preview[1] == b"real staged bytes"
    a.bind_to_turn(session_id=SESSION, turn_id="turn-1", attachment_ids=[record["id"]])
    sent = a.model_attachments_for_turn(session_id=SESSION, turn_id="turn-1")
    assert [entry["text"] for entry in sent] == ["real staged bytes"]
