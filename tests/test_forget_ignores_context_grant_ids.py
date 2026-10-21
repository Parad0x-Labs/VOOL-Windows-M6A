"""A forgotten word never matches a chat's own context grant ids.

Measured 2026-10-21: in chat openclaw:c3c3... "Forget 21." refused every later "Remember ..." there. The memory write
fence checked the write's access policy as words, and that chat's grant id is "grant-e92129353f..." (a hash of the
chat and its source, the same every day). tests/test_remember_again_after_forget_served.py forgets the day of the
month, so it failed on every day whose number is in one of those ids. This pins the cause for any day: forget a piece
of the chat's own grant id, and other facts are still remembered while the forgotten one stays refused.
"""
from __future__ import annotations

import re

import pytest

from core.context_scope import ContextAccessPolicy
from core.persistent_memory import maybe_handle_memory_command
from tests.test_question_date_time_leg_20261002 import _profile

OWNER_LOCAL = {"surface": "cli", "platform": "cli"}
CHAT = "openclaw:" + "c3" * 10
REFUSED = "You asked me to forget that in this chat, so I won't keep it here."


@pytest.fixture(autouse=True)
def own_home(tmp_path, runtime_storage_reset):
    # Forgets live in the home's ledger: a private home keeps them out of every later test in the process.
    import core.runtime_continuity as continuity
    import core.runtime_paths as paths
    import storage.db as database
    from storage.migrations import run_migrations

    previous = paths._VOOL_HOME_OVERRIDE
    previous_db = database.active_default_db_path()
    previous_continuity_db = continuity._DB_PATH_OVERRIDE
    profile = _profile(tmp_path)
    paths.configure_runtime_home(profile)
    try:
        db_path = profile / "data" / "vool_web0_v2.db"
        database.configure_default_db_path(db_path)
        continuity.configure_runtime_continuity_db_path(db_path)
        run_migrations()
        yield profile
    finally:
        continuity.configure_runtime_continuity_db_path(previous_continuity_db)
        database.configure_default_db_path(previous_db)
        paths.configure_runtime_home(previous)


def _say(text: str, chat: str = CHAT) -> str:
    ContextAccessPolicy.for_request(session_id=chat, source_context=OWNER_LOCAL)
    handled, reply = maybe_handle_memory_command(text, session_id=chat, source_context=OWNER_LOCAL)
    assert handled, text
    return reply


def _grant_digits(chat: str) -> str:
    policy = ContextAccessPolicy.for_request(session_id=chat, source_context=OWNER_LOCAL)
    ids = [grant.grant_id for grant in policy.grants]
    assert ids and all(re.fullmatch(r"grant-[0-9a-f]{24}", grant_id) for grant_id in ids), ids
    for grant_id in ids:
        digits = re.search(r"[0-9]{2,}", grant_id.removeprefix("grant-"))
        if digits:
            return digits.group(0)[:2]
    pytest.fail(f"no two digits in a row in {ids}")


def test_forgetting_a_number_inside_the_chats_grant_id_keeps_other_facts():
    number = _grant_digits(CHAT)
    assert "Locked in" in _say(f"remember that my locker is {number}")
    assert "Forget applied" in _say(f"forget {number}")

    assert "Locked in" in _say("remember that my favourite colour is teal")


def test_the_forgotten_number_itself_is_still_refused_in_that_chat():
    number = _grant_digits(CHAT)
    _say(f"remember that my locker is {number}")
    _say(f"forget {number}")

    assert _say(f"remember that my locker is {number}").startswith(REFUSED)
