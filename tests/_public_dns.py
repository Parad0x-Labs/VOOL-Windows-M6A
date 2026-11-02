"""Answer DNS for the public hosts the download-lane tests name, so they run without a network.

The machine-download lane resolves every hostname through `core.null_dial.is_ssrf_safe_url`,
which fails CLOSED when resolution fails. On a host without DNS (an offline laptop, a sandboxed
runner) every fixture that names `example.com` was therefore refused by the address guard before
the gate under test was reached: a genuine download read as "stopped reaching the lane", the
remote-fetch veto was never consulted, and the guard's own control case read as wrong.

`pin_public_test_hosts` answers only the names listed here, with each name's real public address,
and passes every other lookup to the real resolver. The SSRF guard itself still runs: it still
parses the URL, still resolves through `socket.getaddrinfo`, and still classifies the address it
gets back. `.invalid` names still fail to resolve and internal literals never reach DNS, so every
refusal these tests pin keeps its meaning. The answers are the same with or without a network,
so CI and an offline machine see one result.
"""

from __future__ import annotations

import socket

import pytest

#: Each public name a download-lane fixture uses, with that name's real public address.
PUBLIC_TEST_HOSTS = {
    "example.com": "93.184.215.14",
    "raw.githubusercontent.com": "185.199.108.133",
}


def pin_public_test_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    real_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(host, port, *args, **kwargs):
        address = PUBLIC_TEST_HOSTS.get(str(host).lower().rstrip("."))
        return real_getaddrinfo(address or host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
