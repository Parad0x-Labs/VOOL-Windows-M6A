"""The installed-slice harness names its target as the address 127.0.0.1, never as "localhost".

"localhost" is a name: the hosts file or a resolver decides where it goes, so it is not a promise
of loopback. The smoke always passes http://127.0.0.1:<port>, so nothing else is needed.
"""
from __future__ import annotations

import pytest

from tools import installed_slices


def test_a_localhost_url_is_refused_before_anything_is_opened():
    with pytest.raises(ValueError):
        installed_slices._get_json("http://localhost:11435/healthz")


def test_the_command_line_refuses_a_localhost_base(tmp_path):
    with pytest.raises(SystemExit):
        installed_slices.main([
            "--base-url", "http://localhost:11435",
            "--expected-commit", "0123456789abcdef0123456789abcdef01234567",
            "--out", str(tmp_path / "slices.json"),
        ])
    assert not (tmp_path / "slices.json").exists()
