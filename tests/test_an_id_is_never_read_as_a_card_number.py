"""An id such as a pending action's uuid is never read as a card number.

Measured 2026-10-27 (CI run 40552232190, tests (5)): "approve move <action id>" was answered "I didn't save your card
number..." instead of running the move. The action id is a uuid4, and the card-number rule found a grouped, Luhn-valid
digit run inside it ("e19d3678-3904-4936-9867-c00328ca7f14" holds "3678-3904-4936-9867"); 47 of 200,000 random uuids
read as a card number. A uuid is an id, never a card number; card numbers the user writes are still kept out of memory.
"""
from __future__ import annotations

import uuid

import pytest

from core.secret_redaction import contains_personal_secret, redact_personal_secrets, redactable_personal_kinds

# each holds a digit run the card rule matched (found with the measurement above)
CARD_SHAPED_IDS = (
    "e19d3678-3904-4936-9867-c00328ca7f14",
    "8f9b6428-4447-4336-844f-58ab8a374b14",
    "cc975653-5980-4065-ab62-31e5065f27ff",
    "134913b7-cc46-4877-8006-384211fd3c40",
    "6ceb584f-0737-4878-8130-884e1169c06c",
)


@pytest.mark.parametrize("action_id", CARD_SHAPED_IDS)
def test_an_approval_naming_an_action_id_carries_no_card_number(action_id):
    for text in (f"approve move {action_id}", f"approve move {action_id.upper()}", f"my card id is {action_id}"):
        assert redactable_personal_kinds(text) == (), text
        assert not contains_personal_secret(text), text
        assert redact_personal_secrets(text) == text


def test_no_random_uuid_reads_as_a_card_number():
    flagged = [u for u in (str(uuid.uuid4()) for _ in range(20000)) if redactable_personal_kinds(f"approve move {u}")]
    assert flagged == []


@pytest.mark.parametrize("text", [
    "My bank card number is 4111 1111 1111 1111.",
    "use 4111 1111 1111 1111 for it",
    "visa 4111-1111-1111-1111 exp 03/28",
    "my credit card is 1234567812345678",
])
def test_a_card_number_beside_an_id_is_still_kept_out(text):
    beside = f"approve move {CARD_SHAPED_IDS[0]}. {text}"
    assert redactable_personal_kinds(beside) == ("card_number",)
    assert CARD_SHAPED_IDS[0] in redact_personal_secrets(beside)
    assert "[redacted-card-number]" in redact_personal_secrets(beside)


@pytest.mark.parametrize("action_id", CARD_SHAPED_IDS)
@pytest.mark.parametrize("shape", [
    "approve move {id} 4111 1111 1111 1111",
    "4111 1111 1111 1111 {id}",
    "credit card {id} 4111111111111111",
])
def test_a_card_number_a_space_after_an_id_is_still_kept_out(action_id, shape):
    # the card-run rule lets a space join digit groups: the card must not be swallowed into a run that starts in the id
    text = shape.format(id=action_id)
    assert redactable_personal_kinds(text) == ("card_number",), text
    redacted = redact_personal_secrets(text)
    assert action_id in redacted and "[redacted-card-number]" in redacted, redacted
    assert "4111" not in redacted.replace(action_id, ""), redacted


def test_no_card_number_beside_a_random_uuid_escapes():
    escaped = [u for u in (str(uuid.uuid4()) for _ in range(2000))
               if "4111" in redact_personal_secrets(f"approve move {u} 4111 1111 1111 1111").replace(u, "")]
    assert escaped == []


@pytest.mark.parametrize("action_id", CARD_SHAPED_IDS)
@pytest.mark.parametrize("shape", ["{id}4111 1111 1111 1111", "4111 1111 1111 1111{id}", "{id}-4111-1111-1111-1111"])
def test_a_card_number_touching_an_id_is_still_kept_out(action_id, shape):
    # a digit touching the id must not hide the id from the mask, or the card from the scan
    text = "approve move " + shape.format(id=action_id)
    redacted = redact_personal_secrets(text)
    assert action_id in redacted and "[redacted-card-number]" in redacted, redacted
    assert "4111" not in redacted.replace(action_id, ""), redacted
