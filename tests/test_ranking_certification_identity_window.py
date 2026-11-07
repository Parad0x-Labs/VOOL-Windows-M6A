"""Candidate ranking reads authorship certification through the fence's own short cache.

Steve job 44 (5 Nov 2026) left the cold chat-startup turn 0.7 s short of its 15 s answer wait on Windows. A
served cold run counted 31 certification identity requests (``/api/version``, ``/api/tags``, ``/api/show``),
each a spawned provider-HTTP worker. 30 of them came from ranking's best-effort
``local_manifest_authorship_certified``, which called ``certification_status`` directly for every candidate on
every turn, past the 30 s verdict cache that ``author_certification`` (the authoritative fence) already keeps
for exactly this read. Ranking now asks the fence: the same model at the same destination is read once per
window, and a lane at another destination is its own read.
"""
from __future__ import annotations

import pytest

from core import final_answer_authorship as faa
from core import local_model_tool_certification as cert
from storage.model_provider_manifest import ModelProviderManifest

_IDENTITY = "adapters.openai_compatible_adapter.OpenAICompatibleAdapter.tool_certification_runtime_identity"


def _manifest(model: str = "qwen3:4b", base_url: str = "http://127.0.0.1:11434") -> ModelProviderManifest:
    return ModelProviderManifest(
        provider_name="ollama-local",
        model_name=model,
        source_type="http",
        adapter_type="openai_compatible",
        license_name="test",
        license_url_or_reference="test",
        weight_location="external",
        redistribution_allowed=False,
        runtime_dependency="ollama",
        runtime_config={"base_url": base_url},
        metadata={},
        enabled=True,
    )


@pytest.fixture()
def reads(monkeypatch):
    calls: list[str] = []
    clock = [500.0]

    def _identity(self):
        calls.append(self.manifest.model_name)
        return {"backend_version": "0.21.2", "model_digest": "d" * 12}

    monkeypatch.setattr(_IDENTITY, _identity)
    monkeypatch.setattr(faa, "_now", lambda: clock[0])
    monkeypatch.setattr(cert, "latest_certification_run", lambda **_kw: None)
    faa._CERT_CACHE.clear()
    yield calls, clock
    faa._CERT_CACHE.clear()


def test_ranking_reads_within_the_window_share_one_identity_read(reads) -> None:
    calls, clock = reads

    first = faa.local_manifest_authorship_certified(_manifest())
    clock[0] += faa._CERTIFICATION_CACHE_TTL_SECONDS / 2
    second = faa.local_manifest_authorship_certified(_manifest())

    assert calls == ["qwen3:4b"]
    assert first is second is False  # no run on record: not certified, read the same both times


def test_a_ranking_read_older_than_the_window_reads_again(reads) -> None:
    calls, clock = reads

    faa.local_manifest_authorship_certified(_manifest())
    clock[0] += faa._CERTIFICATION_CACHE_TTL_SECONDS + 0.01
    faa.local_manifest_authorship_certified(_manifest())

    assert len(calls) == 2


def test_another_model_or_destination_is_its_own_read(reads) -> None:
    calls, _clock = reads

    faa.local_manifest_authorship_certified(_manifest())
    faa.local_manifest_authorship_certified(_manifest(model="qwen3:0.6b"))
    faa.local_manifest_authorship_certified(_manifest(base_url="http://127.0.0.1:11500"))

    assert len(calls) == 3


def test_ranking_and_the_fence_agree_from_one_read(reads, monkeypatch) -> None:
    calls, _clock = reads
    verified = {"state": "verified", "run_id": "r1"}
    monkeypatch.setattr(cert, "latest_certification_run", lambda **_kw: None)
    monkeypatch.setattr(
        cert, "certification_status", lambda manifest, **_kw: (calls.append(manifest.model_name), verified)[1]
    )

    assert faa.local_manifest_authorship_certified(_manifest()) is True
    assert faa.author_certification(_manifest()).certified is True
    assert calls == ["qwen3:4b"]


def test_a_lane_the_probe_cannot_reach_still_ranks_as_certified(reads, monkeypatch) -> None:
    calls, _clock = reads
    monkeypatch.setattr(cert, "certification_applies", lambda _m: False)

    assert faa.local_manifest_authorship_certified(_manifest()) is True
    assert calls == []


def test_a_failing_read_never_breaks_ranking(reads, monkeypatch) -> None:
    def _broken(*_a, **_kw):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(faa, "author_certification", _broken)

    assert faa.local_manifest_authorship_certified(_manifest()) is True
