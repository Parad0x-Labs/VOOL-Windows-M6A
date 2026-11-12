"""On Windows, answer a request to read a 7z or RAR archive before any lane or model is asked.

Claims sweep (Windows, a chat bound to a marked folder): "open the 7z archive \"data.7z\"" was read by the
operator lane as a move (its move words include "archive"), so the reply was a refusal to move the file
into VOOL's own `archive_outbox` folder; "show me the contents of <folder>\\data.7z" went to the machine
read lane, which answered with the list of folders it reads (Desktop, Downloads, Documents); "read
data.7z" went to the model, which can only guess at what the archive holds.

VOOL reads a 7z or RAR archive only with an external decoder, and only inside the decoder confinement
(`core.artifact_readers`, `ReaderSpec.external_decoder`). Windows has no such confinement
(`core.artifact_readers._sandbox.confinement_available`), so no lane can read one there. A read of one
gets that answer here, in the readers' own words (`artifact_readers.confinement_refusal`), with nothing
opened. A format the readers can read on this host, a turn that carries the archive as an attachment
(the attachment door answers it), and any request that is not a read (move, delete, rename, create) are
left as they were. Off Windows nothing changes.
"""

from __future__ import annotations

import re

#: The read words a request opens with (after "please", "can you" and the like). Anchored at the
#: message's start, so a sentence that only mentions an archive is not read as a request to read it.
_READ_RE = re.compile(
    r"^\s*(?:(?:please|pls|ok(?:ay)?|now|can\s+you|could\s+you|would\s+you|vool)[\s,]+)*"
    r"(?:read|open|show(?:\s+me)?|list|extract|unzip|unpack|decompress|inspect|view|display|cat|print"
    r"|summari[sz]e|look\s+(?:at|inside|in(?:to)?)|peek\s+(?:at|inside|in(?:to)?)"
    r"|what(?:'s|’s|\s+is|\s+are|s)\s+(?:in(?:side)?|the\s+contents?\s+of)"
    r"|tell\s+me\s+what(?:'s|’s|\s+is)\s+in(?:side)?)\b",
    re.IGNORECASE,
)
#: An archive file name: a word that ends in `.7z` or `.rar`.
_ARCHIVE_RE = re.compile(r"(?P<name>[^\s\"'`<>|*?\\/]*\.(?P<ext>7z|rar))(?![\w.-])", re.IGNORECASE)


def windows_archive_read_refusal(text: str, *, source_context: dict | None = None) -> str:
    """On Windows, the plain answer to a one-line request to read a 7z or RAR archive when no reader can open
    one on this host; "" otherwise, and always off Windows (the write gate's seam)."""
    from core import windows_write_gate

    if not windows_write_gate.active():
        return ""
    body = str(text or "").strip()
    if not body or len(body.splitlines()) > 1 or not _READ_RE.match(body):
        return ""
    found = list(_ARCHIVE_RE.finditer(body))
    if not found or len({match.group("ext").lower() for match in found}) != 1:
        return ""
    from core.execution.constants import bound_chat_attachment_present

    if bound_chat_attachment_present(source_context):
        return ""  # the attachment door answers an attached archive
    from core import artifact_readers

    spec = artifact_readers.spec_for_extension("." + found[0].group("ext").lower())
    if spec is None or spec.available(passive=True):
        return ""
    name = found[0].group("name")
    refusal = artifact_readers.confinement_refusal(spec, name=f"`{name}`")
    if refusal is not None:
        reason = refusal.message
    else:
        reason = f"`{name}` is {spec.label}, and VOOL has no reader for it on this computer ({spec.blocked_reason()})."
    return f"{reason} Nothing was opened. Extract it with another program and I can read the files it holds."


__all__ = ["windows_archive_read_refusal"]
