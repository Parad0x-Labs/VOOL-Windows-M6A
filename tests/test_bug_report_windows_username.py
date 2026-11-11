"""B8: the bug-report scrubber knows the Windows login name (USERNAME), not only POSIX USER/LOGNAME."""
from __future__ import annotations

import pytest

from core.bug_report import scanner


@pytest.fixture
def windows_env(monkeypatch):
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("LOGNAME", raising=False)
    monkeypatch.setenv("USERNAME", "Quillbert")
    scanner.reset_dynamic_patterns_for_tests()
    yield
    scanner.reset_dynamic_patterns_for_tests()


def test_the_windows_login_is_masked_outside_a_path(windows_env) -> None:
    masked, hits = scanner.mask_local_identifiers("Logged in as Quillbert, then quillbert again.")
    assert "Quillbert" not in masked and "quillbert" not in masked
    assert masked.count("[redacted-user]") == 2
    assert hits >= 2


def test_the_windows_login_fails_the_outbound_scan(windows_env) -> None:
    rules = {f.rule for f in scanner.scan_text("my account is QUILLBERT")}
    assert "local_username" in rules


def test_a_name_that_only_contains_the_login_is_left_alone(windows_env) -> None:
    masked, _ = scanner.mask_local_identifiers("Quillberts and xQuillbert are other words")
    assert masked == "Quillberts and xQuillbert are other words"


def test_posix_user_matching_stays_case_sensitive(monkeypatch) -> None:
    monkeypatch.setenv("USER", "Quillbert")
    monkeypatch.delenv("LOGNAME", raising=False)
    monkeypatch.delenv("USERNAME", raising=False)
    scanner.reset_dynamic_patterns_for_tests()
    try:
        masked, _ = scanner.mask_local_identifiers("Quillbert quillbert")
        assert masked == "[redacted-user] quillbert"
        assert sum(1 for rule, _ in scanner._dynamic_local_patterns() if rule == "local_username") == 1
    finally:
        scanner.reset_dynamic_patterns_for_tests()
