"""Short connection lifetimes must not cache namespace or grant authority."""
from __future__ import annotations

import contextlib
import sqlite3

import pytest

from core import context_namespace as ns
from core.context_scope import ContextAccessPolicy
from storage import db


@pytest.fixture
def store(tmp_path):
    original = db.active_default_db_path()
    db.configure_default_db_path(tmp_path / "context.db")
    try:
        yield tmp_path
    finally:
        db.configure_default_db_path(original)


def _policy(chat="target", **context):
    return ContextAccessPolicy.for_request(session_id=chat, source_context=context)


def _track(monkeypatch, trace=None):
    original = ns.get_connection
    opened = []

    def tracked():
        conn = original()
        if trace is not None:
            conn.set_trace_callback(trace.append)
        opened.append(conn)
        return conn

    monkeypatch.setattr(ns, "get_connection", tracked)
    return opened


def _released(opened):
    assert opened
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


def test_policy_connection_budget_on_first_and_repeated_requests(store, monkeypatch):
    trace = []
    opened = _track(monkeypatch, trace)
    for _ in range(2):
        start = len(opened)
        trace.clear()
        policy = _policy(_owner_local=True, _trusted_project_id="project-a")
        assert policy.project_id == "project-a"
        assert policy.has_grant("user_profile", "profile:confirmed")
        assert policy.has_grant("action_receipt", "chat:target")
        assert len(opened) - start == 3, "policy repeats schema/connection work"
        assert sum("SELECT chat_id, project_id" in sql for sql in trace) == 5
        assert sum("SELECT grant_id, chat_id" in sql for sql in trace) == 1
        _released(opened)


def test_namespace_creation_owns_one_connection(store, monkeypatch):
    opened = _track(monkeypatch)
    ns.ensure_chat_namespace("target", grant_confirmed_profile=True)
    assert len(opened) == 1
    _released(opened)


@pytest.mark.parametrize("length", [235, 236, 240])
def test_derived_receipt_source_obeys_public_identifier_bound(store, monkeypatch, length):
    opened = _track(monkeypatch)
    chat = "x" * length
    if length + len("chat:") > 240:
        with pytest.raises(ValueError, match="invalid source_id"):
            ns.ensure_chat_namespace(chat)
        # Creation commits first, as before, but an invalid grant must not persist.
        assert ns.load_chat_namespace(chat) is not None
        assert not ns.list_context_imports(chat)
    else:
        ns.ensure_chat_namespace(chat)
        assert ns.list_context_imports(chat)[0].source_id == "chat:" + chat
    _released(opened)


def test_revoked_foreign_import_not_reactivated_by_new_policy(store):
    ns.ensure_chat_namespace("source")
    _policy()
    ns.grant_context_import("target", scope="chat", source_id="chat:source")
    assert "source" in _policy().imported_chat_ids
    assert ns.revoke_context_import("target", scope="chat", source_id="chat:source")
    assert "source" not in _policy().imported_chat_ids


@pytest.mark.parametrize("state", ["archived", "deleted"])
def test_new_policy_does_not_restore_inactive_namespace_or_revoked_grants(store, state):
    _policy(_owner_local=True)
    ns.set_chat_namespace_state("target", state)
    ns.revoke_context_import("target", scope="user_profile", source_id="profile:confirmed")
    ns.revoke_context_import("target", scope="action_receipt", source_id="chat:target")
    policy = _policy(_owner_local=True)
    assert policy.namespace_state == state
    assert not policy.grants


@pytest.mark.parametrize("context", [{"_owner_local": False}, {"_owner_local": True, "is_group": True}])
def test_remote_or_group_request_cannot_create_profile_grant(store, context):
    assert not _policy(**context).has_grant("user_profile", "profile:confirmed")


def test_project_identity_and_reassignment_are_fresh(store):
    assert _policy(_owner_local=True, _trusted_project_id="project-a").project_id == "project-a"
    assert _policy(_owner_local=True, _trusted_project_id="forged").project_id == "project-a"
    ns.grant_context_import("target", scope="project", source_id="project:project-a")
    ns.set_chat_namespace_project("target", "project-b")
    policy = _policy()
    assert policy.project_id == "project-b"
    assert not policy.has_grant("project", "project:project-a")


def test_store_switch_does_not_reuse_schema_namespace_or_grants(store, monkeypatch):
    _policy(_owner_local=True, _trusted_project_id="first")
    opened = _track(monkeypatch)
    db.configure_default_db_path(store / "other.db")
    policy = _policy(_owner_local=False)
    assert policy.project_id == ""
    assert not policy.has_grant("user_profile", "profile:confirmed")
    _released(opened)
    db.configure_default_db_path(store / "context.db")
    assert _policy().project_id == "first"


def test_source_lifecycle_is_live_even_on_existing_policy(store):
    ns.ensure_chat_namespace("source")
    _policy()
    ns.grant_context_import("target", scope="chat", source_id="chat:source")
    policy = _policy()
    assert "source" in policy.imported_chat_ids
    ns.set_chat_namespace_state("source", "deleted")
    assert "source" not in policy.imported_chat_ids


@pytest.mark.parametrize("fault", ["schema", "namespace", "grant", "grant_list"])
def test_sql_failure_refuses_policy_and_releases_all_connections(store, monkeypatch, fault):
    original = ns.get_connection
    opened = []

    class FailingConnection:
        def __init__(self):
            self.conn = original()
            opened.append(self.conn)

        def executescript(self, sql):
            if fault == "schema":
                raise sqlite3.OperationalError("injected schema denial")
            return self.conn.executescript(sql)

        def execute(self, sql, params=()):
            match = {"namespace": "INSERT OR IGNORE INTO context_namespaces",
                     "grant": "INSERT INTO context_import_grants",
                     "grant_list": "SELECT grant_id, chat_id"}
            if fault in match and match[fault] in sql:
                raise sqlite3.OperationalError("injected " + fault + " denial")
            return self.conn.execute(sql, params)

        def commit(self):
            return self.conn.commit()

        def close(self):
            return self.conn.close()

    monkeypatch.setattr(ns, "get_connection", FailingConnection)
    with pytest.raises(sqlite3.OperationalError, match="injected"):
        _policy(_owner_local=True)
    _released(opened)


@pytest.mark.parametrize("second_grant", [False, True])
def test_grant_and_final_namespace_reads_observe_external_commits(store, monkeypatch, second_grant):
    # Delete the namespace after its first grant committed. The next grant must
    # re-read lifecycle, not reuse the earlier active row or a read transaction.
    original = ns.get_connection
    opened = []

    class InterleavedConnection:
        def __init__(self):
            self.conn = original()
            self.grant_written = False
            opened.append(self.conn)

        def executescript(self, sql):
            return self.conn.executescript(sql)

        def execute(self, sql, params=()):
            self.grant_written |= "INSERT INTO context_import_grants" in sql
            return self.conn.execute(sql, params)

        def commit(self):
            self.conn.commit()
            if self.grant_written:
                self.grant_written = False
                with contextlib.closing(db.get_connection()) as external:
                    external.execute("UPDATE context_namespaces SET lifecycle_state='deleted' WHERE chat_id='target'")
                    external.execute("UPDATE context_import_grants SET revoked_at='external' WHERE chat_id='target'")
                    external.commit()

        def close(self):
            self.conn.close()

    monkeypatch.setattr(ns, "get_connection", InterleavedConnection)
    if second_grant:
        with pytest.raises(ValueError, match="active chat"):
            _policy(_owner_local=True)
    else:
        policy = _policy(_owner_local=False)
        assert policy.namespace_state == "deleted"
        assert not policy.grants
    _released(opened)
