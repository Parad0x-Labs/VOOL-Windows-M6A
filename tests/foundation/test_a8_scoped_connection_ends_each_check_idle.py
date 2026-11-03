"""A scoped availability connection ends every check without an open transaction.

Inside availability_read_scope() one connection serves every check. The legacy-tombstone migration
writes on it and swallows a failed rollback; a transaction left open there would hold the write lock
for the rest of the scope, where a per-check connection used to be closed at once.
"""

from __future__ import annotations

import core.finalization as fin
from tests.foundation.test_a8_privacy_pass003 import a8_env


def test_a_transaction_left_open_by_a_check_is_rolled_back(a8_env, monkeypatch):
    def leaves_a_write_open(conn):
        conn.execute("BEGIN IMMEDIATE")

    monkeypatch.setattr(fin, "_migrate_legacy_digest_tombstones", leaves_a_write_open)
    with fin.availability_read_scope():
        fin.payload_availability_for_hash("sha256:" + "0" * 64)
        scoped = fin._AVAILABILITY_READ_SCOPE.get()[1]
        assert scoped is not None
        assert not scoped.in_transaction, "the shared connection kept the write lock after a check"
        # The next check still works on the scope.
        assert fin.payload_availability_for_hash("sha256:" + "1" * 64) is None
