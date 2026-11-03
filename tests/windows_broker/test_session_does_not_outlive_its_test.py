"""A broker host one test registers is gone before the next test starts (conftest restore).

The order of these two tests is the point: the first leaves a host registered the way
apps.vool_api_server.main() does, and the second must still see the process unwired.
"""

from __future__ import annotations

from core.windows_broker import session


class _Host:
    pass


def test_a_test_that_registers_a_host_like_main_does() -> None:
    session.register(_Host())  # type: ignore[arg-type]
    assert session.wired()


def test_the_next_test_does_not_inherit_that_host() -> None:
    assert not session.wired()
