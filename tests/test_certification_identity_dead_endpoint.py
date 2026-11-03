"""A loopback model endpoint that refuses connections is read once per window, not per call.

Measured on Windows (Steve, port 5c1157c9, 3 Nov 2026): a refused loopback connect costs about
2 s there (Linux refuses at once). ``certification_status`` reads the backend identity -- version,
tags and show, three requests -- for every candidate manifest, and ranking plus the authorship
fence call it on every turn. With no Ollama listening, a hermetic gauntlet child spent 95 s of
"server ready -> first turn" in those refused connects, and the turn's provider deadline expired
before the model was ever called ("model wire not reached on a real turn", 63 Windows reds).

The invariant: once an identity read fails to CONNECT, further identity reads of that same base
make no request until a short window passes; the answer in the meantime is the same ``unknown``
a failed read gives. Any other failure is not remembered, a different base is unaffected, and the
endpoint is read again once the window ends.
"""

from __future__ import annotations

from typing import Any

import pytest

import adapters.openai_compatible_adapter as oca
from core.model_registry import ModelProviderManifest


def _manifest(base_url: str = "http://127.0.0.1:11434", model: str = "qwen-test:4b") -> ModelProviderManifest:
    return ModelProviderManifest(
        provider_name="ollama-local",
        model_name=model,
        source_type="http",
        adapter_type="openai_compatible",
        runtime_config={"base_url": base_url},
        metadata={"runtime_family": "ollama"},
    )


class _Transport:
    """Stands in for core.provider_http: records every request, raises what it is told to."""

    def __init__(self, error: BaseException | None) -> None:
        self.error = error
        self.calls: list[str] = []

    def _do(self, url: str, **_: Any) -> Any:
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        raise AssertionError("no live endpoint in this test")

    def get(self, url: str, **kwargs: Any) -> Any:
        return self._do(url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self._do(url, **kwargs)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [1000.0]
    monkeypatch.setattr(oca.time, "monotonic", lambda: now[0])
    oca._IDENTITY_UNREACHABLE.clear()
    yield now
    oca._IDENTITY_UNREACHABLE.clear()


def _install(monkeypatch: pytest.MonkeyPatch, error: BaseException | None) -> _Transport:
    transport = _Transport(error)
    monkeypatch.setattr(oca.requests, "get", transport.get)
    monkeypatch.setattr(oca.requests, "post", transport.post)
    return transport


def _refused() -> BaseException:
    return oca.requests.exceptions.ConnectionError("[WinError 10061] connection refused")


def test_a_refused_endpoint_is_connected_to_once_across_repeated_identity_reads(
    monkeypatch: pytest.MonkeyPatch, clock: list[float]
) -> None:
    transport = _install(monkeypatch, _refused())
    first = oca.OpenAICompatibleAdapter(_manifest()).tool_certification_runtime_identity()
    for model in ("a:1b", "b:2b", "c:3b", "d:4b"):
        again = oca.OpenAICompatibleAdapter(_manifest(model=model)).tool_certification_runtime_identity()
        assert again["backend_version"] == "unknown"
    assert first["backend_version"] == "unknown"
    assert len(transport.calls) == 1, transport.calls


def test_the_backend_version_read_alone_also_skips_a_refused_endpoint(
    monkeypatch: pytest.MonkeyPatch, clock: list[float]
) -> None:
    transport = _install(monkeypatch, _refused())
    for _ in range(3):
        assert oca.OpenAICompatibleAdapter(_manifest()).tool_certification_backend_version() == "unknown"
    assert len(transport.calls) == 1, transport.calls


def test_the_endpoint_is_read_again_once_the_window_passes(
    monkeypatch: pytest.MonkeyPatch, clock: list[float]
) -> None:
    transport = _install(monkeypatch, _refused())
    oca.OpenAICompatibleAdapter(_manifest()).tool_certification_backend_version()
    clock[0] += oca._IDENTITY_UNREACHABLE_TTL_S + 0.01
    oca.OpenAICompatibleAdapter(_manifest()).tool_certification_backend_version()
    assert len(transport.calls) == 2, transport.calls


def test_a_different_base_is_still_read(monkeypatch: pytest.MonkeyPatch, clock: list[float]) -> None:
    transport = _install(monkeypatch, _refused())
    oca.OpenAICompatibleAdapter(_manifest()).tool_certification_backend_version()
    oca.OpenAICompatibleAdapter(_manifest("http://127.0.0.1:11500/v1")).tool_certification_backend_version()
    assert [url.split("/api/")[0] for url in transport.calls] == [
        "http://127.0.0.1:11434",
        "http://127.0.0.1:11500",
    ]


def test_a_failure_that_is_not_a_connect_failure_is_not_remembered(
    monkeypatch: pytest.MonkeyPatch, clock: list[float]
) -> None:
    transport = _install(monkeypatch, ValueError("malformed body"))
    oca.OpenAICompatibleAdapter(_manifest()).tool_certification_backend_version()
    oca.OpenAICompatibleAdapter(_manifest()).tool_certification_backend_version()
    assert len(transport.calls) == 2, transport.calls
