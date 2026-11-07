"""A prompt capture is written only where it can be made owner-only; otherwise nothing is captured."""

from __future__ import annotations

import pytest

from core import prompt_debug
from core.owner_only import OwnerOnlyUnavailable
from tests.test_prompt_capture_default import _payload, capture_home


def _unavailable(path, *args, **kwargs):
    raise OwnerOnlyUnavailable(path, "no_persistent_acls")


def test_no_capture_is_written_when_the_folder_cannot_be_made_private(capture_home, monkeypatch) -> None:
    monkeypatch.setattr(prompt_debug, "ensure_private_dir", _unavailable)
    prompt_debug.dump_outbound_prompt(_payload("a secret the user pasted"))
    assert not capture_home.exists()


def test_a_capture_that_cannot_be_secured_does_not_break_the_turn(capture_home, monkeypatch) -> None:
    monkeypatch.setattr(prompt_debug, "require_owner_only", _unavailable)
    prompt_debug.dump_outbound_prompt(_payload())  # never raises


@pytest.mark.parametrize("hook", ["ensure_private_dir", "require_owner_only"])
def test_the_capture_routes_through_core_owner_only(capture_home, monkeypatch, hook) -> None:
    calls: list[str] = []
    real = getattr(prompt_debug, hook)

    def spy(path, *args, **kwargs):
        calls.append(str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(prompt_debug, hook, spy)
    prompt_debug.dump_outbound_prompt(_payload())
    assert calls, f"{hook} was never called for the capture"
