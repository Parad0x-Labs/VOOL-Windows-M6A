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
import contextlib
import hashlib
import logging
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.effect_reconciliation import EffectOutcomeUnknown as _VoolOutcomeUnknown

from . import session
from .protocol import BatchNotRestored, BrokerRefusal, EffectOutcomeUnknown

logger = logging.getLogger(__name__)


class OutcomeUnknown(_VoolOutcomeUnknown, EffectOutcomeUnknown):
    """A change request was written and its outcome is unknown (section 8): VOOL's own
    EffectOutcomeUnknown, so an A6-reserved intent holding a claim is recorded `unknown`, not safe to
    retry, and never a plain failure; and the client's, for callers that catch that. `batch` is the
    broker's report when a batch could not all be put back."""

    def __init__(self, *, reason: str, detail: str = "", batch: dict[str, Any] | None = None) -> None:
        super().__init__(reason=reason, detail=detail)
        self.batch = batch or {}


def _unclear(detail: str) -> OutcomeUnknown:
    return OutcomeUnknown(reason="broker_reply_unclear", detail=detail)


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
    """The flagged folder holding `target`, its parent folders resolved, and the path inside it ("" for
    the folder)."""
    text = str(target)
    if "\x00" in text:
        raise Refused("invalid_path", "the path holds a NUL character")
    try:
        # The folders above the target are resolved; the target's own name never is. A by-path
        # writer replaced or moved a link at the target, never what it points at, so the broker is
        # given the link's name and its walk refuses it (reparse_point).
        absolute = os.path.abspath(text)
        parent, name = os.path.split(absolute)
        real = os.path.join(os.path.realpath(parent), name) if name else os.path.realpath(absolute)
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


@contextlib.contextmanager
def _logged(operation: str, *paths: Path) -> Iterator[list[str]]:
    """One INFO line per change routed through the broker: the operation, the path(s) and the result,
    never the bytes and never a hash of them (a short content hash lets anyone holding the log confirm
    a guess of a short secret; Security Architect, 9 Nov 2026). Security Architect question SA1
    (captest round 1, 2026-11-09): the runner found no record on the VM that a marked-folder write
    went through VoolBroker, because nothing logged a routed change. The caller appends what was done
    to the yielded list; a refusal and an unknown outcome are logged with their code before they
    propagate."""
    done: list[str] = []
    where = " -> ".join(str(path) for path in paths)
    try:
        yield done
    except Refused as refused:
        logger.info("Windows broker %s %s: refused (%s)", operation, where, refused.code)
        raise
    except OutcomeUnknown as unknown:
        logger.info("Windows broker %s %s: outcome unknown (%s)", operation, where, unknown.reason)
        raise
    except Exception as exc:
        logger.info("Windows broker %s %s: failed (%s)", operation, where, type(exc).__name__)
        raise
    logger.info("Windows broker %s %s: done%s", operation, where, f" ({'; '.join(done)})" if done else "")


def _parent(rel: str) -> str:
    return rel.rpartition("\\")[0]


class _Call:
    """One connection for one call: the folder checked, then its root opened, then the changes."""

    def __init__(self, target: Path | None = None, *, placed: tuple[Any, str] | None = None) -> None:
        host, status = _status()
        self.folder, self.rel = placed if placed is not None else _place(target, status)  # type: ignore[arg-type]
        self.client = session._client_for(status, host.secret)
        self.created: list[str] = []

    def __enter__(self) -> _Call:
        try:
            try:
                self.client.connect()
            except BrokerRefusal as refusal:
                raise Refused(refusal.code, refusal.message) from refusal
            except (ConnectionError, EffectOutcomeUnknown, OSError, ValueError) as exc:
                raise Refused("broker_unavailable", f"the broker could not be reached ({type(exc).__name__})") from exc
            self.root_id = self.root_for(self.folder)
        except BaseException:
            self.client.close()
            raise
        return self

    def root_for(self, folder: Any) -> str:
        """`folder`'s root on this connection, after its final path is checked (section 4)."""
        try:
            answer = self.client.request("flagged_path", flagged_id=folder.id)
            path = answer.get("path")
            if not isinstance(path, str) or os.path.normcase(path) != os.path.normcase(folder.path):
                raise Refused("flagged_moved", f"the flagged folder {folder.path} is now at {path!r}")
            root_id = self.client.request("open_root", flagged_id=folder.id, rel="").get("root_id")
            if not isinstance(root_id, str) or not root_id:
                raise Refused("broker_unavailable", "the broker opened no root")
            return root_id
        except BrokerRefusal as refusal:
            raise Refused(refusal.code, refusal.message) from refusal
        except (ConnectionError, EffectOutcomeUnknown, OSError, ValueError) as exc:
            # Nothing so far changes a file: a lost reply here changed nothing either.
            raise Refused("broker_unavailable", f"the broker could not be asked ({type(exc).__name__})") from exc

    def __exit__(self, *exc_info: object) -> None:
        self.client.close()

    def change(self, operation: str, *, count: int = 1, **arguments: Any) -> list[dict[str, Any]]:
        """One change request and its `count` results. A lost reply (EffectOutcomeUnknown, and
        BatchNotRestored for a batch not all put back) propagates: it may have run."""
        try:
            reply = self.client.request(operation, root_id=self.root_id, **arguments)
        except BatchNotRestored as stopped:
            left = ", ".join(str(item) for item in stopped.batch.get("left") or []) or "unknown"
            raise OutcomeUnknown(reason="broker_batch_not_restored",
                                 detail=f"{stopped.code} ({stopped.cause or ''}); still out of place: {left}",
                                 batch=dict(stopped.batch)) from stopped
        except EffectOutcomeUnknown as lost:
            raise OutcomeUnknown(reason="broker_reply_lost", detail=str(lost)) from lost
        except BrokerRefusal as refusal:  # BatchRestored too: put back in full
            raise Refused(refusal.code, refusal.message, created=tuple(self.created)) from refusal
        except ConnectionError as exc:  # nothing was written
            raise Refused("broker_unavailable", "the broker could not be reached", created=tuple(self.created)) from exc
        results = reply.get("results")
        if not isinstance(results, list) or len(results) != count or not all(isinstance(row, dict) for row in results):
            raise _unclear("broker reply for a change has no result for each operation")
        return results

    def ensure_directory(self, rel: str) -> None:
        result = self.change("ensure_directory", rel=rel)[0]
        created = result.get("created")
        if not isinstance(created, list) or not all(isinstance(item, str) for item in created):
            raise _unclear("broker reply for ensure_directory has no created list")
        self.created.extend(created)


def ensure_directory(target: Path) -> tuple[str, ...]:
    """Create `target` and any missing parents through the broker; the folders it created."""
    with _logged("ensure_directory", target) as done, _Call(target) as call:
        if call.rel:
            call.ensure_directory(call.rel)
        done.append(f"created {len(call.created)} folder(s)")
        return tuple(call.created)


def write_file(target: Path, data: bytes, expect: str) -> Written:
    """Write `data` to `target` through the broker, refused unless the file is what `expect` says.

    Missing parent folders are created first, as the by-path writers did."""
    with _logged("write_file", target) as done, _Call(target) as call:
        if not call.rel:
            raise Refused("invalid_path", "the target is the flagged folder itself, not a file in it")
        parent = _parent(call.rel)
        if parent:
            call.ensure_directory(parent)
        result = call.change("write_file", rel=call.rel, data=base64.b64encode(data).decode("ascii"), expect=expect)[0]
        written = _written(result, call.rel, data, tuple(call.created))
        done.append(f"{len(data)} bytes")
        return written


def delete(target: Path) -> None:
    """Delete `target` through the broker: a file, or a folder that is empty (`not_empty` otherwise).
    The broker sets it aside in its journal, so a stopped delete is put back."""
    with _logged("delete", target), _Call(target) as call:
        if not call.rel:
            raise Refused("invalid_path", "the target is the flagged folder itself")
        result = call.change("delete", rel=call.rel)[0]
        if result.get("op") not in (None, "delete"):
            raise _unclear("broker reply for delete has the wrong shape")


def rename(source: Path, destination: Path) -> None:
    """Move `source` to `destination` through the broker's `rename` (section 6): the source is held
    and renamed onto the held destination folder, never replacing an existing destination. Across two
    flagged folders the second is checked and opened as `to_root_id`; across volumes the broker refuses
    `cross_volume`. The destination folder must already exist."""
    with _logged("rename", source, destination):
        _rename(source, destination)


def _rename(source: Path, destination: Path) -> None:
    _host, status = _status()
    folder, rel = _place(source, status)
    to_folder, to_rel = _place(destination, status)
    if not rel or not to_rel:
        raise Refused("invalid_path", "a flagged folder itself cannot be moved, or moved onto")
    with _Call(placed=(folder, "")) as call:
        extra: dict[str, Any] = {}
        if to_folder.id != folder.id:
            extra["to_root_id"] = call.root_for(to_folder)
        result = call.change("rename", rel=rel, to=to_rel, replace=False, **extra)[0]
        if result.get("op") not in (None, "rename"):
            raise _unclear("broker reply for rename has the wrong shape")




def _written(result: dict[str, Any], rel: str, data: bytes, created: tuple[str, ...]) -> Written:
    sha = result.get("sha256")
    file_id = result.get("file_id")
    if result.get("op") not in (None, "write_file") or sha != hashlib.sha256(data).hexdigest() or not isinstance(file_id, str):
        raise _unclear("broker reply for write_file does not match the bytes sent")
    return Written(rel, file_id, sha, created)


def write_files(changes: list[tuple[Path, bytes, str]]) -> list[Written]:
    """Write several files as one `apply_batch` (section 6): every `expect` is checked before any
    file changes, and a batch that stops part way is put back by the broker's journal.

    All files must lie in one flagged folder. Missing parent folders of new files are made first, one
    `ensure_directory` each, as the by-path writer made them."""
    results = apply_batch([(target, data, expect) for target, data, expect in changes])
    return [result for result in results if isinstance(result, Written)]


def apply_batch(changes: list[tuple[Path, bytes | None, str]]) -> list[Written | str]:
    """Apply writes and deletes as one `apply_batch`: `(target, data, expect)` writes `data`, and
    `(target, None, "")` deletes the file (set aside in the broker's journal; the broker's delete
    carries no `expect`, so the caller checks the file just before, as the by-path writers did).

    Every write's `expect` is checked before any file changes, and a batch that stops part way is put
    back by the broker's journal. All targets must lie in one flagged folder. Returns a `Written` per
    write and the deleted path per delete, in order."""
    if not changes:
        return []
    with _logged("apply_batch", *[target for target, _data, _expect in changes]) as done:
        results = _apply_batch(changes)
        done.append(f"{sum(isinstance(item, Written) for item in results)} written, "
                    f"{sum(isinstance(item, str) for item in results)} deleted")
        return results


def _apply_batch(changes: list[tuple[Path, bytes | None, str]]) -> list[Written | str]:
    _host, status = _status()
    placed = [(_place(target, status), data, expect) for target, data, expect in changes]
    folder = placed[0][0][0]
    if any(place[0].id != folder.id for place, _data, _expect in placed):
        raise Refused("spans_flagged", "the files are not all inside one folder the broker holds")
    if any(not place[1] for place, _data, _expect in placed):
        raise Refused("invalid_path", "a target is the flagged folder itself, not a file in it")
    with _Call(placed=(folder, "")) as call:
        parents: list[str] = []
        for (_folder, rel), data, expect in placed:
            parent = _parent(rel)
            if data is not None and expect == "absent" and parent and parent not in parents:
                parents.append(parent)
        for parent in parents:
            call.ensure_directory(parent)
        ops = [{"op": "delete", "rel": rel} if data is None else
               {"op": "write_file", "rel": rel, "data": base64.b64encode(data).decode("ascii"), "expect": expect}
               for (_folder, rel), data, expect in placed]
        results = call.change("apply_batch", count=len(ops), ops=ops)
        done: list[Written | str] = []
        for result, ((_folder, rel), data, _expect) in zip(results, placed, strict=True):
            if data is not None:
                done.append(_written(result, rel, data, tuple(call.created)))
            elif result.get("op") != "delete":
                raise _unclear("broker reply for delete has the wrong shape")
            else:
                done.append(rel)
        return done


__all__ = [
    "OutcomeUnknown",
    "Refused",
    "Written",
    "apply_batch",
    "delete",
    "ensure_directory",
    "expect_for",
    "expect_for_reviewed",
    "rename",
    "routed",
    "write_file",
    "write_files",
]
