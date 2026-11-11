"""Windows: a read the user types against a share or a device path is refused in plain words.

"read \\\\localhost\\c$\\Windows\\win.ini", "show me \\\\.\\PhysicalDrive0" and "open \\\\?\\C:\\" asked for a
file's contents, and no message lane claimed them: the read lane (`machine_file_read_path`) never
looks a share or a device up, by design, so the turn went to the model. The tool door then refused
the call if the model made one, and the model paraphrased that refusal or never made the call, so
the user got a vague answer. Measured on Windows 11, 11 Nov 2026 (claims sweep, 170f6572). Nothing was
read either way.

`non_local_read_target` reads the message as typed with the read lane's own cue, and names the first
path in it that is a share or a device. The front door answers that path with the same refusal
sentence the tool door gives. It is lexical: nothing is looked up, resolved or opened. Off Windows it
returns ("", "") at its first statement.

Only shares and devices are refused here. A root-relative or drive-relative spelling is not a full
local path, but it is also how a POSIX path or a sentence fragment reads, and the read and list
tools' own door already refuses it in the same words.
"""
from __future__ import annotations

from core import windows_path_policy

_TRAILING = ".,;:!?)"


def non_local_read_target(text: str) -> tuple[str, str]:
    """On Windows, the first path a read in this message names that is a share or a device, with its
    kind; ("", "") otherwise, and always off Windows. Nothing is looked up."""
    if not windows_path_policy.active():
        return "", ""
    from core.agent_runtime.write_root_honesty import _TYPED_WORD_RE, typed_path_kind
    from core.execution.constants import _TYPED_FILE_CONTENT_CUE_RE, _TYPED_FILE_OTHER_INTENT_RE

    body = str(text or "")
    if not _TYPED_FILE_CONTENT_CUE_RE.search(body) or _TYPED_FILE_OTHER_INTENT_RE.search(body):
        return "", ""
    for match in _TYPED_WORD_RE.finditer(body):
        group = "quoted" if match.group("quoted") is not None else "word"
        candidate = str(match.group(group) or "").strip().rstrip(_TRAILING)
        if not candidate:
            continue
        kind = typed_path_kind(body, match.start(group), candidate)
        if kind in windows_path_policy.NETWORK_KINDS:
            return candidate, kind
    return "", ""


__all__ = ["non_local_read_target"]
