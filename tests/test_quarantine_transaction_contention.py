"""Both serialized orders of a stale deletion and a newer synthetic paste."""
from __future__ import annotations

import threading

import pytest

from core.credential_intelligence import _state_lock as locks
from core.credential_intelligence.provider_registry import default_registry
from core.credential_intelligence.store import CredentialStore, IntakeRefusedError
from core.cross_process_lock import LockUnavailable, PublicationLock
from tests.first_run_pact_rig import pact_rig
from tests.test_quarantine_destination_isolation import LATER_KEY, THIRD_KEY


@pytest.mark.parametrize("first", ["delete", "paste"])
def test_newer_paste_survives_both_contended_transaction_orders(pact_rig, monkeypatch, first):
    registry = default_registry()
    store = CredentialStore(registry)
    descriptor = registry.get("custom")
    endpoint = "http://127.0.0.1:1/v1"
    store.save_quarantined(descriptor, LATER_KEY, endpoint=endpoint)
    old = store.quarantine_snapshot("custom")
    assert old is not None
    acquired = threading.Event()
    contended = threading.Event()
    release = threading.Event()
    errors = []
    outcomes = {}

    class OrderedLock(PublicationLock):
        def __enter__(self):
            try:
                result = super().__enter__()
            except LockUnavailable:
                contended.set()
                raise
            if threading.current_thread().name == "first-credential-transaction":
                acquired.set()
                if not release.wait(10):
                    super().__exit__(None, None, None)
                    raise TimeoutError("test did not release the first transaction")
            return result

    monkeypatch.setattr(locks, "PublicationLock", OrderedLock)

    def run(action):
        try:
            if action == "paste":
                CredentialStore(registry).save_quarantined(descriptor, THIRD_KEY, endpoint=endpoint)
                outcomes[action] = "saved"
            else:
                try:
                    result = store.delete_quarantined("custom", snapshot=old)
                    assert result.removed
                    outcomes[action] = "deleted"
                except IntakeRefusedError as exc:
                    assert exc.status == "quarantine_changed"
                    outcomes[action] = "refused"
        except Exception as exc:
            errors.append(exc)

    second = "paste" if first == "delete" else "delete"
    workers = [
        threading.Thread(target=run, args=(first,), name="first-credential-transaction"),
        threading.Thread(target=run, args=(second,), name="second-credential-transaction"),
    ]
    try:
        workers[0].start()
        assert acquired.wait(10), "the first transaction never acquired its lock"
        workers[1].start()
        assert contended.wait(10), "the competing transaction never attempted the held lock"
    finally:
        release.set()
        for worker in workers:
            if worker.ident is not None:
                worker.join(15)
    assert not any(worker.is_alive() for worker in workers), "a transaction deadlocked"
    assert not errors, errors
    assert outcomes == {"paste": "saved", "delete": "deleted" if first == "delete" else "refused"}
    fresh = store.quarantine_snapshot("custom")
    assert fresh is not None
    assert fresh.secret == THIRD_KEY
    assert fresh.endpoint == endpoint
    assert fresh.epoch > old.epoch
    assert fresh.operation_id != old.operation_id
