"""A provider's 4xx/5xx reaches the operator as the provider's own reason, not a bare status line.

Measured on the served composer, 2026-09-02: three cloud attempts for one attachment turn each
surfaced as "400 Client Error: Bad Request for url: .../chat/completions" -- the status line,
and nothing of the JSON body in which the provider had said what was wrong. The adapter now
carries a bounded, secret-redacted excerpt of that body on the same exception class.

The original HTTP error is deliberately not chained: its URL can contain a credential. The
provider's reason belongs in the sanitized message, not in a raw exception cause or context.
"""

from __future__ import annotations

import traceback
import uuid
from typing import Any

import pytest
import requests

from adapters import openai_compatible_adapter as adapter


class _Response:
    def __init__(self, status: int, text: str) -> None:
        self.status_code = status
        self.text = text
        self.reason = "Bad Request"
        self.url = "https://openrouter.ai/api/v1/chat/completions"

    def raise_for_status(self) -> None:
        raise requests.HTTPError(f"{self.status_code} Client Error: {self.reason} for url: {self.url}", response=self)


def test_the_body_excerpt_rides_the_same_exception_class_and_is_redacted() -> None:
    body = '{"error":{"message":"image_url is not supported by this endpoint","code":400}, "key":"sk-or-v1-abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"}'
    with pytest.raises(requests.HTTPError) as info:
        adapter._raise_for_status_with_cause(_Response(400, body))
    text = str(info.value)
    assert text.startswith("400 Client Error")
    assert "provider said:" in text and "image_url is not supported" in text
    assert "sk-or-v1-abcdef0123456789" not in text
    assert info.value.__cause__ is None
    assert info.value.__context__ is None


@pytest.mark.parametrize("status", [400, 429, 503])
@pytest.mark.parametrize("body_kind", ["empty", "json", "unreadable"])
def test_sanitized_provider_failures_never_chain_credential_bearing_errors(
    monkeypatch, status: int, body_kind: str,
) -> None:
    token = str(uuid.uuid4())
    api_key = "sk-" + uuid.uuid4().hex + uuid.uuid4().hex
    body = "" if body_kind == "empty" else f'{{"error":"invalid payload", "key":"{api_key}"}}'

    class Response(_Response):
        def __getattribute__(self, name):
            if body_kind == "unreadable" and name == "text":
                raise UnicodeError("synthetic body read failure")
            return super().__getattribute__(name)

    response = Response(status, body)
    response.url = f"https://provider.example.test/proxy/{token}/v1/chat/completions"
    response.headers = {"Retry-After": "17"}
    original = requests.HTTPError(
        f"{status} Client Error: rejected {api_key} for url: {response.url}", response=response,
    )

    def fail_status():
        raise original

    monkeypatch.setattr(response, "raise_for_status", fail_status)
    with pytest.raises(requests.HTTPError) as caught:
        adapter._raise_for_status_with_cause(response)

    sanitized = caught.value
    rendered = "".join(traceback.format_exception(sanitized))
    assert token in str(original) and api_key in str(original), "control must contain real-shaped synthetic secrets"
    assert sanitized is not original
    assert sanitized.response is response
    assert sanitized.__cause__ is None and sanitized.__context__ is None
    assert token not in rendered and api_key not in rendered
    assert token not in repr(sanitized) and api_key not in repr(sanitized)
    assert str(status) in str(sanitized)
    assert ("provider said:" in str(sanitized)) is (body_kind == "json")
    assert ("[retry_after=17s]" in str(sanitized)) is (status == 429)


def test_a_body_that_is_empty_leaves_the_original_error_untouched() -> None:
    with pytest.raises(requests.HTTPError) as info:
        adapter._raise_for_status_with_cause(_Response(502, ""))
    assert "provider said" not in str(info.value)


def test_an_oversized_body_is_bounded() -> None:
    with pytest.raises(requests.HTTPError) as info:
        adapter._raise_for_status_with_cause(_Response(400, "x" * 10_000))
    assert len(str(info.value)) < 500


def test_a_healthy_response_passes_through() -> None:
    class _Ok:
        text = ""

        def raise_for_status(self) -> None:
            return None

    assert adapter._raise_for_status_with_cause(_Ok()) is None


def test_every_chat_call_site_uses_the_cause_aware_check() -> None:
    import inspect

    source = inspect.getsource(adapter)
    # The two prewarm probes keep the plain check (they never produce an operator-facing reason);
    # every chat/stream call site must go through the helper.
    body = source.split("def _ollama_prewarm_request", 1)[1]
    chat_sites = body.count("_raise_for_status_with_cause(response)")
    assert chat_sites >= 4, chat_sites
    _ = Any
