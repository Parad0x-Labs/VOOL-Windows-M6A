"""Guard the real historical schema independently of current code and Git availability."""
import hashlib
import subprocess

import pytest

from tests import test_legacy_migration_repair as legacy


def test_historical_schema_has_the_original_digest_and_shape():
    schema = legacy._legacy_schema_sql()
    assert hashlib.sha256(schema.encode()).hexdigest() == "e2ab521f62ffbeae46b8dd367f606025fcc0d7395bf7471c62df084e2a5f52a3"
    assert "retry_idempotency_key" not in schema
    assert "CREATE TABLE IF NOT EXISTS runtime_attempts" in schema


@pytest.mark.parametrize("replacement", ["", "CREATE TABLE invented (id TEXT);", "\n-- changed historical input\n"])
def test_changed_historical_fixture_fails_before_database_creation(tmp_path, monkeypatch, replacement):
    fixture = tmp_path / "changed.sql"
    fixture.write_text(replacement, encoding="utf-8")
    monkeypatch.setattr(legacy, "_LEGACY_SCHEMA_PATH", fixture)
    with pytest.raises(RuntimeError, match="historical migration fixture integrity mismatch"):
        legacy._build_legacy_database(tmp_path / "test.db")
    assert not (tmp_path / "test.db").exists()


def test_historical_fixture_needs_neither_git_nor_current_schema(monkeypatch):
    from storage import migrations

    def unavailable(*args, **kwargs):
        raise AssertionError("historical schema must not depend on Git")

    monkeypatch.setattr(subprocess, "run", unavailable)
    monkeypatch.setattr(migrations, "SCHEMA_SQL", "CREATE TABLE invented (id TEXT);")
    schema = legacy._legacy_schema_sql()
    assert hashlib.sha256(schema.encode()).hexdigest() == "e2ab521f62ffbeae46b8dd367f606025fcc0d7395bf7471c62df084e2a5f52a3"
