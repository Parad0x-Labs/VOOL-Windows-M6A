"""The bootstrap's profile read must obey its caller's explicit authority flag."""
from types import SimpleNamespace

import pytest

from core import bootstrap_context
from core.identity_manager import load_active_persona
from core.task_router import create_task_record


@pytest.mark.parametrize("allow_profile", [False, True])
def test_bootstrap_reads_profile_only_when_authorized(monkeypatch, allow_profile):
    calls = []

    def profile_lines():
        calls.append(True)
        return ["Saved response preference: PROFILE_AUTHORITY_SENTINEL."]

    monkeypatch.setattr(bootstrap_context, "profile_context_lines_for_session", profile_lines)
    prompt = "Inspect the project files."
    items = bootstrap_context.build_bootstrap_context(
        persona=load_active_persona("default"), task=create_task_record(prompt),
        classification={"task_class": "system_design", "risk_flags": []},
        interpretation=SimpleNamespace(raw_text=prompt, normalized_text=prompt, reconstructed_text=prompt),
        session_id="profile-authority-test", include_private_context=False,
        include_user_profile_context=allow_profile, include_history_context=False,
    )
    assert bool(calls) is allow_profile
    assert any("PROFILE_AUTHORITY_SENTINEL" in item.content for item in items) is allow_profile
    assert any(item.source_type == "persona" for item in items)
    assert any(item.source_type == "policy" for item in items)
