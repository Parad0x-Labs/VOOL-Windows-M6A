"""Windows: a key save that cannot be made private answers in one plain sentence and saves nothing.

`core.owner_only.write_private_file` raises `OwnerOnlyUnavailable` when the credential file cannot be
made owner-only (a volume with no ACL support, say), and writes nothing. The Settings key save and the
`cloud key ...` chat command each turn that into one plain sentence: never a traceback, never a "Saved".

The Windows branch is switched on through `owner_only._SEAL_SECRETS`, with the seal refused.
"""
from __future__ import annotations

import pytest

from core import owner_only
from tests.operator_profile_rig import body_json, make_agent, profile_env_generator, served_post

KEY = "sk-or-v1-" + "0" * 64


@pytest.fixture()
def profile_env(tmp_path, monkeypatch):
    yield from profile_env_generator(tmp_path, monkeypatch)


@pytest.fixture
def refusing(monkeypatch):
    monkeypatch.setattr(owner_only, "_SEAL_SECRETS", True)
    monkeypatch.setattr(owner_only, "_REPAIRED", set())
    monkeypatch.setattr(owner_only, "make_owner_only", lambda path, directory=False: owner_only.OwnerOnlyReport(False, "no_acl_support"))


def _stored() -> bool:
    from core import credential_store

    try:
        return bool(credential_store.has_credential("llm.cloud.openrouter"))
    except Exception:
        return False


def test_a_settings_key_save_that_cannot_be_sealed_is_one_plain_sentence(profile_env, refusing) -> None:
    response = served_post(make_agent(), "/api/settings/credentials", {"name": "llm.cloud.openrouter", "value": KEY})
    payload = body_json(response)
    assert int(response.status) == 503, payload
    assert payload["error"] == owner_only.OWNER_ONLY_SAVE_REFUSED, payload
    assert payload["secure_storage_code"] == "owner_only_unavailable", payload
    assert "Traceback" not in str(payload) and KEY not in str(payload)
    assert not _stored()


def test_a_chat_cloud_key_save_that_cannot_be_sealed_is_one_plain_sentence(profile_env, refusing) -> None:
    from core.agent_runtime.fast_command_surface import _seal_cloud_key

    reply = _seal_cloud_key(KEY)
    assert reply.startswith(owner_only.OWNER_ONLY_SAVE_REFUSED), reply
    assert KEY not in reply
    assert not _stored()


def test_control_with_sealing_working_the_settings_key_save_succeeds(profile_env, monkeypatch) -> None:
    monkeypatch.setattr(owner_only, "_SEAL_SECRETS", True)
    monkeypatch.setattr(owner_only, "_REPAIRED", set())
    response = served_post(make_agent(), "/api/settings/credentials", {"name": "llm.cloud.openrouter", "value": KEY})
    assert int(response.status) == 200, body_json(response)
    assert _stored()
