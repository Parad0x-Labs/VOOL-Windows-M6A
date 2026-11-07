"""Windows: a memory write survives a moment's sharing violation instead of failing the turn.

T57 Windows full suite (run 42396620843, port 72e47e52):
test_concurrent_remember_forget_preserves_unrelated_entries failed with
PermissionError(13, 'Access is denied'). On Windows `os.replace` is refused while another reader holds
the target open without delete sharing, and opening a file is refused while a replace is swapping it
in. Those handles close within moments. The memory store now goes through core/windows_sharing_retry.py,
the bounded retry the liquefy store and the disposable-folder list already use. Off Windows a refusal
is still a real refusal and is raised at once. The Windows rule is driven here through the helper's
`_IS_WINDOWS` flag, as tests/test_first_run_pact_windows_sharing_violation.py does.
"""

from __future__ import annotations

import builtins
import io
import os
from pathlib import Path

import pytest

from core import windows_sharing_retry
from core.context_namespace import ensure_chat_namespace
from core.memory import revocation
from core.memory.entries import add_memory_fact, forget_memory, list_memory_entries, resolve_memory_access_policy
from core.memory.files import memory_entries_path, memory_path
from core.runtime_paths import configure_runtime_home

CHAT = "sharing-violation-chat"


@pytest.fixture
def home(tmp_path: Path):
    configure_runtime_home(tmp_path / "runtime-home")
    memory_path().parent.mkdir(parents=True, exist_ok=True)
    yield tmp_path / "runtime-home"
    configure_runtime_home(None)


def _policy():
    ensure_chat_namespace(CHAT, grant_confirmed_profile=True)
    return resolve_memory_access_policy(chat_id=CHAT)


def _texts(policy) -> list[str]:
    return [str(row.get("text") or "") for row in list_memory_entries(access_policy=policy, limit=50)]


def _refuse_replace_once(monkeypatch, target: Path) -> list[str]:
    """The first os.replace onto ``target`` is refused like a Windows sharing violation."""
    real = os.replace
    refused: list[str] = []

    def _replace(src, dst, *args, **kwargs):
        if not refused and Path(dst) == target:
            refused.append(str(dst))
            raise PermissionError(13, "Access is denied")
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", _replace)
    return refused


def _refuse_open_once(monkeypatch, target: Path) -> list[str]:
    """The first read of ``target`` is refused like an open during another writer's replace."""
    real_open = io.open
    refused: list[str] = []

    def _open(file, mode="r", *args, **kwargs):
        if not refused and "r" in mode and isinstance(file, (str, os.PathLike)) and Path(file) == target:
            refused.append(str(file))
            raise PermissionError(13, "Access is denied")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(io, "open", _open)
    monkeypatch.setattr(builtins, "open", _open)
    return refused


def test_a_remember_survives_a_replace_refused_for_a_moment(home, monkeypatch) -> None:
    monkeypatch.setattr(windows_sharing_retry, "_IS_WINDOWS", True)
    policy = _policy()
    assert add_memory_fact("Anchor fact about copper kettles.", session_id=CHAT, access_policy=policy)
    refused = _refuse_replace_once(monkeypatch, memory_entries_path())

    assert add_memory_fact("Harbor lantern alpha burns steadiest.", session_id=CHAT, access_policy=policy)

    assert refused, "the store's replace was never refused, so this proves nothing"
    texts = _texts(policy)
    assert any("Harbor lantern alpha" in t for t in texts) and any("copper kettles" in t for t in texts)


def test_a_forget_survives_a_replace_refused_for_a_moment(home, monkeypatch) -> None:
    monkeypatch.setattr(windows_sharing_retry, "_IS_WINDOWS", True)
    policy = _policy()
    assert add_memory_fact("my sister's bakery is called Flourstone Bakehouse.", session_id=CHAT, access_policy=policy)
    assert add_memory_fact("Anchor fact about copper kettles.", session_id=CHAT, access_policy=policy)
    refused = _refuse_replace_once(monkeypatch, memory_entries_path())

    forget_memory("Flourstone Bakehouse", access_policy=policy)

    assert refused, "the store's replace was never refused, so this proves nothing"
    texts = _texts(policy)
    assert not any("Flourstone" in t for t in texts) and any("copper kettles" in t for t in texts)


def test_a_read_refused_for_a_moment_still_sees_the_store(home, monkeypatch) -> None:
    monkeypatch.setattr(windows_sharing_retry, "_IS_WINDOWS", True)
    policy = _policy()
    assert add_memory_fact("Anchor fact about copper kettles.", session_id=CHAT, access_policy=policy)
    refused = _refuse_open_once(monkeypatch, memory_entries_path())

    texts = _texts(policy)

    assert refused, "the store's read was never refused, so this proves nothing"
    assert any("copper kettles" in t for t in texts)


def test_off_windows_a_refused_replace_is_raised_at_once(home, monkeypatch) -> None:
    monkeypatch.setattr(windows_sharing_retry, "_IS_WINDOWS", False)
    policy = _policy()
    assert add_memory_fact("Anchor fact about copper kettles.", session_id=CHAT, access_policy=policy)
    real = os.replace
    attempts: list[str] = []

    def _always_refused(src, dst, *args, **kwargs):
        if Path(dst) == memory_entries_path():
            attempts.append(str(dst))
            raise PermissionError(13, "Access is denied")
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", _always_refused)
    with pytest.raises(PermissionError):
        add_memory_fact("Harbor lantern bravo burns steadiest.", session_id=CHAT, access_policy=policy)
    assert len(attempts) == 1


def test_the_erasure_ledger_replace_is_retried_on_windows(tmp_path, monkeypatch) -> None:
    """The ledger's own same-directory replace (MoveFileExW on Windows) gets the same bounded retry."""
    monkeypatch.setattr(windows_sharing_retry, "_IS_WINDOWS", True)
    monkeypatch.setattr(windows_sharing_retry, "_DELAYS", (0.0, 0.0, 0.0))
    monkeypatch.setattr(revocation, "_WINDOWS", True)
    calls: list[tuple[str, str]] = []

    def _move(source, target) -> None:
        calls.append((str(source), str(target)))
        if len(calls) == 1:
            raise PermissionError(13, "Access is denied")
        os.replace(source, target)

    monkeypatch.setattr(revocation, "_move_file_replace", _move)
    source, target = tmp_path / "ledger.json.tmp", tmp_path / "ledger.json"
    source.write_text("{}", encoding="utf-8")

    revocation._replace_file(source, target)

    assert len(calls) == 2 and target.read_text(encoding="utf-8") == "{}"
