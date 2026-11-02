"""Single writes through the Windows broker (docs/WINDOWS_BROKER.md sections 4, 6 and 8; Phase C slice C2).

Once this process has started the broker (`session.wired`), workspace.write_file, machine.write_file,
workspace.ensure_directory and machine.ensure_directory are done on Windows by the broker, relative
to the handle it holds for the flagged folder, never by path:
- the target is placed in the flagged folder that holds its final path (the deepest one, when
  entries nest), as the gate placed it; a target in none is refused;
- before the change, the broker is asked that folder's current final path (`flagged_path`), and a
  mismatch is refused, so the folder the gate checked is the one the broker writes in;
- `write_file` always carries `expect`: the hash of the bytes VOOL checked, or `absent` when it found
  no file, so a file changed since VOOL read it is refused `changed_since_check`.

Outcomes (section 8):
- `Refused`: nothing was changed by the refused request (a broker refusal, a batch the broker put
  back, a broker that is absent or cannot be reached, a target that cannot be placed, or a reply lost
  for a request that changes nothing). `created` names folders an earlier request of the same call
  made before a write was refused.
- `EffectOutcomeUnknown` (the client's) propagates unchanged when a change request was written and
  its reply was lost or did not make sense: the change may have happened.
"""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import session
from .protocol import BrokerRefusal, EffectOutcomeUnknown


class Refused(Exception):
    """The broker did not make this change; nothing was changed by the refused request."""

    def __init__(self, code: str, message: str, *, created: tuple[str, ...] = ()) -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message
        self.created = created


@dataclass(frozen=True)
class Written:
    """A file the broker wrote: its path relative to the flagged folder, file id and new hash."""

    rel: str
    file_id: str
    sha256: str
    created: tuple[str, ...] = field(default=())


def routed() -> bool:
    """Whether single writes go through the broker: on Windows, in a process that started it."""
    from core import windows_write_gate

    return windows_write_gate.active() and session.wired()


def expect_for(previous: bytes | None) -> str:
    """`expect` for the bytes VOOL checked, or `absent` when it found no file (section 6)."""
    return "absent" if previous is None else "sha256:" + hashlib.sha256(previous).hexdigest()


def expect_for_reviewed(prior_sha256: str) -> str:
    """`expect` for a reviewed destination's recorded hash ("" meaning no file was there)."""
    prior = str(prior_sha256 or "").strip().lower()
    return "absent" if not prior else "sha256:" + prior


def _status() -> tuple[Any, Any]:
    host = session._host
    status = None if host is None else host.status
    if status is None or not status.running:
        raise Refused("broker_unavailable", "the Windows broker is not running")
    return host, status


def _place(target: Path, status: Any) -> tuple[Any, str]:
    """The flagged folder holding `target`'s final path, and the path inside it ("" for the folder)."""
    text = str(target)
    if "\x00" in text:
        raise Refused("invalid_path", "the path holds a NUL character")
    try:
        real = os.path.realpath(text)
    except (OSError, ValueError) as exc:
        raise Refused("invalid_path", f"the path cannot be resolved ({type(exc).__name__})") from exc
    best = None
    for folder in status.flagged:
        root = os.path.normcase(folder.path.rstrip("\\/"))
        try:
            inside = os.path.commonpath([os.path.normcase(real), root]) == root
        except ValueError:  # another drive
            continue
        if inside and (best is None or len(root) > len(os.path.normcase(best.path.rstrip("\\/")))):
            best = folder
    if best is None:
        raise Refused("outside_flagged", "the target is not inside a folder the broker holds")
    rel = real[len(best.path.rstrip("\\/")):].lstrip("\\/")
    return best, rel.replace("/", "\\")


def _parent(rel: str) -> str:
    return rel.rpartition("\\")[0]


class _Call:
    """One connection for one call: the folder checked, then its root opened, then the changes."""

    def __init__(self, target: Path) -> None:
        host, status = _status()
        self.folder, self.rel = _place(target, status)
        self.client = session._client_for(status, host.secret)
        self.created: list[str] = []

    def __enter__(self) -> _Call:
        try:
            self.client.connect()
            answer = self.client.request("flagged_path", flagged_id=self.folder.id)
            path = answer.get("path")
            if not isinstance(path, str) or os.path.normcase(path) != os.path.normcase(self.folder.path):
                raise Refused("flagged_moved", f"the flagged folder {self.folder.path} is now at {path!r}")
            root_id = self.client.request("open_root", flagged_id=self.folder.id, rel="").get("root_id")
            if not isinstance(root_id, str) or not root_id:
                raise Refused("broker_unavailable", "the broker opened no root")
            self.root_id = root_id
        except BrokerRefusal as refusal:
            self.client.close()
            raise Refused(refusal.code, refusal.message) from refusal
        except (ConnectionError, EffectOutcomeUnknown, OSError, ValueError) as exc:
            # Nothing so far changes a file: a lost reply here changed nothing either.
            self.client.close()
            raise Refused("broker_unavailable", f"the broker could not be asked ({type(exc).__name__})") from exc
        except BaseException:
            self.client.close()
            raise
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.client.close()

    def change(self, operation: str, **arguments: Any) -> dict[str, Any]:
        """One change request. A lost reply (EffectOutcomeUnknown) propagates: it may have run."""
        try:
            reply = self.client.request(operation, root_id=self.root_id, **arguments)
        except BrokerRefusal as refusal:  # BatchRestored too: put back in full
            raise Refused(refusal.code, refusal.message, created=tuple(self.created)) from refusal
        except ConnectionError as exc:  # nothing was written
            raise Refused("broker_unavailable", "the broker could not be reached", created=tuple(self.created)) from exc
        results = reply.get("results")
        if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
            raise EffectOutcomeUnknown("broker reply for a change has no result")
        return results[0]

    def ensure_directory(self, rel: str) -> None:
        result = self.change("ensure_directory", rel=rel)
        created = result.get("created")
        if not isinstance(created, list) or not all(isinstance(item, str) for item in created):
            raise EffectOutcomeUnknown("broker reply for ensure_directory has no created list")
        self.created.extend(created)


def ensure_directory(target: Path) -> tuple[str, ...]:
    """Create `target` and any missing parents through the broker; the folders it created."""
    with _Call(target) as call:
        if call.rel:
            call.ensure_directory(call.rel)
        return tuple(call.created)


def write_file(target: Path, data: bytes, expect: str) -> Written:
    """Write `data` to `target` through the broker, refused unless the file is what `expect` says.

    Missing parent folders are created first, as the by-path writers did."""
    with _Call(target) as call:
        if not call.rel:
            raise Refused("invalid_path", "the target is the flagged folder itself, not a file in it")
        parent = _parent(call.rel)
        if parent:
            call.ensure_directory(parent)
        result = call.change("write_file", rel=call.rel, data=base64.b64encode(data).decode("ascii"), expect=expect)
        sha = result.get("sha256")
        file_id = result.get("file_id")
        if sha != hashlib.sha256(data).hexdigest() or not isinstance(file_id, str):
            raise EffectOutcomeUnknown("broker reply for write_file does not match the bytes sent")
        return Written(call.rel, file_id, sha, tuple(call.created))


__all__ = ["Refused", "Written", "ensure_directory", "expect_for", "expect_for_reviewed", "routed", "write_file"]
