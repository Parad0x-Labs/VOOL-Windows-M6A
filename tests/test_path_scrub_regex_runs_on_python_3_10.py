"""The shared path scrubber must compile on every Python pyproject allows (``>=3.10``).

Possessive quantifiers (``x++``, ``x*+``, ``x?+``) and atomic groups (``(?>...)``) arrived in
Python 3.11; on 3.10 ``re.compile`` raises "multiple repeat", so importing ``core.path_scrub``
(and every redactor built on it) would fail there. CI runs 3.12, so the check reads the pattern
sources instead of relying on a 3.10 run.
"""
from __future__ import annotations

import re

import pytest

from core import path_scrub

_PY311_ONLY = re.compile(r"[+*?}]\+|\(\?>")


_SOURCES = {
    name: value.pattern if isinstance(value, re.Pattern) else value
    for name, value in vars(path_scrub).items()
    if isinstance(value, re.Pattern) or (isinstance(value, str) and not name.startswith("__"))
}


@pytest.mark.parametrize(("name", "source"), list(_SOURCES.items()), ids=list(_SOURCES))
def test_no_pattern_uses_syntax_newer_than_python_3_10(name, source):
    assert not _PY311_ONLY.search(source), f"{name} uses a 3.11-only construct: {source[:120]!r}"


def test_a_long_run_of_separators_stays_fast():
    import time

    started = time.perf_counter()
    for pattern in (path_scrub.WINDOWS_PATH_RE, path_scrub.SPACED_HOME_RE, path_scrub.NETWORK_PATH_RE):
        pattern.findall("\\" * 40000)
        pattern.findall("C:" + "/" * 40000)
    assert time.perf_counter() - started < 5.0
