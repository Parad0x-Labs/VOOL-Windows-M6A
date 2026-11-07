"""Stop must end the local generation, not only hide the answer.

Clean-VM job 52 (port 76afbd36): after Stop the reply showed "(stopped showing this answer)"
within a second, but the RUN badge stayed lit 32 s+ and the next reply waited ~6 minutes. Stop
reaches the backend -- /api/chat/cancel sets the turn's cancel event, which every ModelRequest
reads through ``is_cancelled()`` -- but ordinary chat buffers its answer, so it takes the
NON-streaming Ollama call, and that call looked at the event only before and after one blocking
POST. The generation ran to its end, still holding the local-model slot the next turn queues on.

Ollama stops generating when its client connection closes. These tests stand up a loopback HTTP
server that never answers (a generation that is still going) and record whether the client hung
up: a cancelled call must hang up promptly, raise ``model_call_cancelled`` and free the slot.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from adapters.base_adapter import ModelRequest
from core import local_model_admission, provider_http


class _StillGenerating:
    """A loopback 'Ollama' that reads one request and then never replies."""

    def __init__(self) -> None:
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        self.received = threading.Event()
        self.hung_up = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        conn, _ = self._listener.accept()
        with conn:
            conn.settimeout(30.0)
            data = b""
            while b"\r\n\r\n" not in data:
                data += conn.recv(65536)
            self.received.set()
            try:
                while conn.recv(65536):
                    pass  # a client that hangs up ends this read with b""
            except OSError:
                pass
            self.hung_up.set()

    def close(self) -> None:
        self._listener.close()


@pytest.fixture()
def generating():
    server = _StillGenerating()
    yield server
    server.close()


def _cancel_after_receipt(server: _StillGenerating):
    flag = threading.Event()

    def _flip() -> None:
        if server.received.wait(20.0):
            time.sleep(0.2)
            flag.set()

    threading.Thread(target=_flip, daemon=True).start()
    return flag


def test_a_cancelled_transfer_hangs_up_on_the_provider(generating) -> None:
    flag = _cancel_after_receipt(generating)
    started = time.monotonic()

    with pytest.raises(provider_http.TransferCancelled):
        provider_http.post(
            f"http://127.0.0.1:{generating.port}/api/chat",
            json={"model": "m", "messages": [], "stream": False},
            timeout=(5.0, 60.0),
            cancel_check=flag.is_set,
        )

    assert time.monotonic() - started < 15.0
    assert generating.hung_up.wait(5.0), "the provider still holds an open connection after cancel"


def test_a_transfer_without_a_cancel_check_is_unchanged(generating) -> None:
    # No cancel_check: only the wall-clock deadline ends it, exactly as before.
    with pytest.raises(provider_http.exceptions.Timeout):
        provider_http.post(
            f"http://127.0.0.1:{generating.port}/api/chat",
            json={"model": "m", "messages": [], "stream": False},
            timeout=(5.0, 1.5),
        )


class _Manifest:
    def __init__(self, port: int) -> None:
        self.provider_id = "ollama-local"
        self.provider_name = "ollama"
        self.model_name = "qwen3:8b"
        self.runtime_config = {"base_url": f"http://127.0.0.1:{port}", "timeout_seconds": 60}
        self.metadata = {"runtime_family": "ollama"}


def test_stop_ends_the_buffered_ollama_call_and_frees_the_model_slot(generating, monkeypatch) -> None:
    from adapters.openai_compatible_adapter import OpenAICompatibleAdapter

    monkeypatch.setattr(OpenAICompatibleAdapter, "_gate_local_model_load", lambda self: None)
    adapter = OpenAICompatibleAdapter(_Manifest(generating.port))  # type: ignore[arg-type]
    flag = _cancel_after_receipt(generating)
    request = ModelRequest(task_kind="chat", prompt="tell me a long story", cancel_check=flag.is_set)
    in_flight_before = local_model_admission.lane_snapshot()["in_flight"]
    started = time.monotonic()

    with pytest.raises(RuntimeError, match="model_call_cancelled"):
        adapter._invoke_ollama_chat(request, force_json=False)

    assert time.monotonic() - started < 15.0
    assert generating.hung_up.wait(5.0), "Ollama would keep generating: the connection stayed open"
    assert local_model_admission.lane_snapshot()["in_flight"] == in_flight_before
