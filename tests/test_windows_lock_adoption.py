"""The shard-store and registry-install file locks must hold on Windows too.

``_StoreFileLock`` (core/learning/procedure_shards.py) and ``_filesystem_lock``
(core/registry/native.py) used to import ``fcntl`` directly, so on Windows every
shard promotion, shard update and registry install died with ``ModuleNotFoundError:
No module named 'fcntl'`` (Windows baseline slices 06 C1, 07 C3, 08 C11). Both lock
through ``core.os_file_lock`` — ``fcntl.flock`` on POSIX, ``msvcrt.locking`` on
Windows — so these paths must take and release the real lock on both platforms.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from core import os_file_lock
from core.learning.procedure_shards import (
    ProcedureShardV1,
    _StoreFileLock,
    find_or_create_procedure_shard,
    load_procedure_shards,
    update_procedure_shard,
)

PROCEDURE_ID = "lock-adoption-probe"


def _probe_shard() -> ProcedureShardV1:
    return ProcedureShardV1.create(
        task_class="general",
        title="lock adoption probe",
        preconditions=("the store is redirected to a temp dir",),
        steps=("take the store file lock", "write the shard"),
        tool_receipts=(),
        validation={"kind": "lock-adoption-probe"},
        rollback={"kind": "none"},
        privacy_class="local_private",
        shareability="local_only",
        success_signal="verified_success",
        procedure_id=PROCEDURE_ID,
    )


class ShardStoreWindowsLockTests(unittest.TestCase):
    """The real workflow the 41 slice-06 reds exercise: promote, update, reload."""

    def setUp(self) -> None:
        self.store = Path(tempfile.mkdtemp(prefix="shard-lock-adoption-"))
        self.addCleanup(shutil.rmtree, self.store, ignore_errors=True)
        patcher = mock.patch(
            "core.learning.procedure_shards.data_path",
            lambda *parts: self.store.joinpath(*parts),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_promote_update_and_reload_a_shard_through_the_store_file_lock(self) -> None:
        shard, created = find_or_create_procedure_shard(
            PROCEDURE_ID,
            _probe_shard,
            lambda existing: replace(existing, reuse_count=existing.reuse_count + 1),
        )
        self.assertTrue(created)
        self.assertEqual(shard.procedure_id, PROCEDURE_ID)

        updated = update_procedure_shard(
            PROCEDURE_ID,
            lambda item: replace(item, verified_reuse_count=item.verified_reuse_count + 1),
        )
        self.assertIsNotNone(updated)
        assert updated is not None  # for the type checker only
        self.assertEqual(updated.verified_reuse_count, 1)

        reloaded = [item.procedure_id for item in load_procedure_shards()]
        self.assertEqual(reloaded, [PROCEDURE_ID])

    def test_a_held_store_lock_excludes_a_second_descriptor_until_released(self) -> None:
        shard_path = self.store / "learning" / "procedures" / f"{PROCEDURE_ID}.json"
        lock_file = shard_path.with_suffix(shard_path.suffix + ".lock")
        with _StoreFileLock(shard_path):
            second = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                with self.assertRaises(OSError):
                    os_file_lock.try_lock_exclusive(second)
            finally:
                os.close(second)
        second = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os_file_lock.try_lock_exclusive(second)  # released by __exit__: acquires
            os_file_lock.unlock(second)
        finally:
            os.close(second)


class RegistryInstallWindowsLockTests(unittest.TestCase):
    def test_the_registry_install_lock_holds_and_excludes_a_second_descriptor(self) -> None:
        from core.registry.native import _filesystem_lock

        root = Path(tempfile.mkdtemp(prefix="registry-lock-adoption-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        with _filesystem_lock(root) as scratch:
            lock_file = scratch / "install.lock"
            self.assertTrue(lock_file.exists())
            second = os.open(str(lock_file), os.O_RDWR | os.O_CREAT, 0o600)
            try:
                with self.assertRaises(OSError):
                    os_file_lock.try_lock_exclusive(second)
            finally:
                os.close(second)
        second = os.open(str(scratch / "install.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os_file_lock.try_lock_exclusive(second)  # released on exit: acquires
            os_file_lock.unlock(second)
        finally:
            os.close(second)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
