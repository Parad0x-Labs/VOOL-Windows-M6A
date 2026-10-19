"""Windows integration tests are added with the real broker executable."""

from __future__ import annotations

import os

import pytest


@pytest.mark.skipif(os.name != "nt", reason="requires the Windows broker and named pipes")
def test_client_requires_a_valid_broker_pipe() -> None:
    from core.windows_broker.client import BrokerClient

    with pytest.raises(ValueError, match="pipe name"):
        BrokerClient("not-a-pipe", b"0" * 32, 1)

