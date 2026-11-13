"""Windows Credential Manager as the default key store, driven on Linux through a fake advapi32.

``core.windows_credential_manager._load_advapi32`` is replaced with :class:`FakeAdvapi32`, which
speaks the same five calls (CredWriteW / CredReadW / CredDeleteW / CredEnumerateW / CredFree) on
real ``CREDENTIALW`` structures, so the ctypes marshalling (target names, blob bytes, persist and
type fields, the returned pointers and their frees) is what is under test. Nothing here touches a
real Credential Manager. Every value is an obvious fake.
"""
from __future__ import annotations

import ctypes
import json

import pytest

import core.credential_store as cs
from core import owner_only, runtime_paths
from core import windows_credential_manager as wcm

FAKE_KEY = "sk-or-v1-fake-test-key-0000-1234"
OTHER_FAKE = "fake-brave-key-5678"


def _write_pointer(ptr_obj, address: int) -> None:
    """Store ``address`` into the pointer variable ``ptr_obj`` (what Windows does for an out-param)."""
    ctypes.memmove(
        ctypes.addressof(ptr_obj), ctypes.byref(ctypes.c_void_p(address)), ctypes.sizeof(ctypes.c_void_p)
    )


class FakeAdvapi32:
    """An in-memory Credential Manager with advapi32's calling convention."""

    def __init__(self) -> None:
        self.records: dict[str, dict] = {}
        self.error = 0
        self.live: dict[int, object] = {}
        self.freed = 0
        self.allocated = 0
        self.fail_write: int | None = None
        self.fail_read: int | None = None
        self.fail_enumerate: int | None = None
        self.corrupt_readback = False
        self.enumerate_filters: list[str] = []

    def last_error(self) -> int:
        return self.error

    def _alloc(self, keep: object, address: int) -> None:
        self.live[address] = keep
        self.allocated += 1

    def _make(self, target: str, rec: dict) -> tuple[wcm.CREDENTIALW, object]:
        blob = rec["blob"]
        buf = (ctypes.c_ubyte * max(len(blob), 1)).from_buffer_copy(blob or b"\0")
        cred = wcm.CREDENTIALW()
        cred.Type = rec["type"]
        cred.TargetName = target
        cred.UserName = rec["user"]
        cred.Persist = rec["persist"]
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
        return cred, buf

    def CredWriteW(self, pcred, flags):
        cred = pcred._obj
        if self.fail_write is not None:
            self.error = self.fail_write
            return 0
        blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        if self.corrupt_readback:
            blob = b"something-else"
        self.records[cred.TargetName] = {
            "type": int(cred.Type),
            "persist": int(cred.Persist),
            "user": cred.UserName,
            "comment": cred.Comment,
            "blob": blob,
            "flags": flags,
        }
        return 1

    def CredReadW(self, target, ctype, flags, ppcred):
        if self.fail_read is not None:
            self.error = self.fail_read
            return 0
        rec = self.records.get(target)
        if rec is None or rec["type"] != ctype:
            self.error = wcm.ERROR_NOT_FOUND
            return 0
        cred, buf = self._make(target, rec)
        address = ctypes.addressof(cred)
        self._alloc((cred, buf), address)
        _write_pointer(ppcred._obj, address)
        return 1

    def CredDeleteW(self, target, ctype, flags):
        if target not in self.records:
            self.error = wcm.ERROR_NOT_FOUND
            return 0
        del self.records[target]
        return 1

    def CredEnumerateW(self, filt, flags, pcount, ppcreds):
        self.enumerate_filters.append(filt)
        if self.fail_enumerate is not None:
            self.error = self.fail_enumerate
            return 0
        assert filt.endswith("*")
        prefix = filt[:-1]
        hits = [(t, r) for t, r in sorted(self.records.items()) if t.startswith(prefix)]
        if not hits:
            self.error = wcm.ERROR_NOT_FOUND
            return 0
        made = [self._make(t, r) for t, r in hits]
        array = (wcm.PCREDENTIALW * len(made))(*[ctypes.pointer(cred) for cred, _ in made])
        address = ctypes.addressof(array)
        self._alloc((array, made), address)
        pcount._obj.value = len(made)
        _write_pointer(ppcreds._obj, address)
        return 1

    def CredFree(self, pointer):
        address = ctypes.cast(pointer, ctypes.c_void_p).value
        assert address in self.live, "CredFree of memory the API never handed out"
        del self.live[address]
        self.freed += 1

    def plant(self, target: str, blob: bytes, *, user: str = "x") -> None:
        self.records[target] = {
            "type": wcm.CRED_TYPE_GENERIC, "persist": wcm.CRED_PERSIST_LOCAL_MACHINE,
            "user": user, "comment": None, "blob": blob, "flags": 0,
        }


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("VOOL_HOME", str(tmp_path))
    runtime_paths.configure_runtime_home(tmp_path)
    monkeypatch.delenv("VOOL_KEYCHAIN_ALLOWED", raising=False)
    monkeypatch.setattr(cs, "_WINDOWS_NOTICE_SHOWN", False)
    monkeypatch.setattr(cs, "_MIGRATION_NOTICES", set())
    monkeypatch.setattr(cs, "_REINDEX_NOTICES", set(), raising=False)
    wcm.reset_probe()
    yield
    wcm.reset_probe()
    runtime_paths.configure_runtime_home(None)


@pytest.fixture
def api(monkeypatch):
    fake = FakeAdvapi32()
    monkeypatch.setattr(wcm, "_load_advapi32", lambda: fake)
    return fake


def _installed_home_attended(monkeypatch) -> None:
    """What the installed app is: an attended run on the default home. A pytest process on a
    tmp_path home is neither, so without this it keeps to the vault (rows at the end of the file)."""
    monkeypatch.setattr(cs, "_windows_home_may_use_credential_manager", lambda: True)


@pytest.fixture
def windows(monkeypatch, api):
    """This process is Windows, the store mode is the default, advapi32 is the fake, and the run is
    the installed app's (attended, on the default home)."""
    monkeypatch.delenv("VOOL_CREDENTIAL_STORE", raising=False)
    monkeypatch.setattr(cs.sys, "platform", "win32")
    _installed_home_attended(monkeypatch)
    return api


def _store(api) -> wcm.WindowsCredentialManager:
    return wcm.WindowsCredentialManager(api, service=cs._KC_SERVICE)


def _vault_raw() -> dict:
    path = runtime_paths.data_path("credentials.enc.json")
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


# ------------------------------------------------------------------ the advapi32 layer itself


def test_write_then_read_round_trips_as_a_generic_local_machine_credential(api):
    store = _store(api)
    store.set_password(cs._KC_SERVICE, "llm.cloud.openrouter", FAKE_KEY)

    rec = api.records["VOOL/credentials/llm.cloud.openrouter"]
    assert rec["type"] == wcm.CRED_TYPE_GENERIC == 1
    assert rec["persist"] == wcm.CRED_PERSIST_LOCAL_MACHINE == 2
    assert rec["user"] == "llm.cloud.openrouter"
    assert rec["blob"] == FAKE_KEY.encode("utf-8")
    assert FAKE_KEY not in str(rec["comment"])
    assert store.get_password(cs._KC_SERVICE, "llm.cloud.openrouter") == FAKE_KEY
    assert api.freed == api.allocated == 1 and not api.live


def test_the_blob_is_utf8_bytes_and_non_ascii_survives():
    api = FakeAdvapi32()
    store = _store(api)
    value = "fake-ключ-✓-0001"
    store.set_password(cs._KC_SERVICE, "x.api.default", value)
    assert api.records["VOOL/credentials/x.api.default"]["blob"] == value.encode("utf-8")
    assert store.get_password(cs._KC_SERVICE, "x.api.default") == value


def test_a_blob_that_is_not_utf8_fails_closed_instead_of_guessing():
    api = FakeAdvapi32()
    api.plant("VOOL/credentials/llm.cloud.openrouter", "fake".encode("utf-16-le") + b"\xff\xfe\xff")
    with pytest.raises(wcm.CredentialManagerError):
        _store(api).get_password(cs._KC_SERVICE, "llm.cloud.openrouter")
    assert not api.live


def test_read_of_an_absent_credential_is_none_and_another_error_raises():
    api = FakeAdvapi32()
    store = _store(api)
    assert store.get_password(cs._KC_SERVICE, "llm.cloud.openrouter") is None
    api.fail_read = 5  # ERROR_ACCESS_DENIED
    with pytest.raises(wcm.CredentialManagerError) as caught:
        store.get_password(cs._KC_SERVICE, "llm.cloud.openrouter")
    assert caught.value.winerror == 5


def test_delete_removes_and_an_absent_delete_is_typed_not_found():
    api = FakeAdvapi32()
    store = _store(api)
    store.set_password(cs._KC_SERVICE, "llm.cloud.openrouter", FAKE_KEY)
    store.delete_password(cs._KC_SERVICE, "llm.cloud.openrouter")
    assert api.records == {}
    with pytest.raises(wcm.CredentialNotFound):
        store.delete_password(cs._KC_SERVICE, "llm.cloud.openrouter")


def test_enumerate_lists_only_vool_names_and_frees_the_array():
    api = FakeAdvapi32()
    store = _store(api)
    assert store.list_names(cs._KC_SERVICE) == []
    store.set_password(cs._KC_SERVICE, "llm.cloud.openrouter", FAKE_KEY)
    store.set_password(cs._KC_SERVICE, "search.brave", OTHER_FAKE)
    api.plant("SomeOtherApp/token", b"fake")
    api.plant("VOOL/other-store/thing", b"fake")
    assert store.list_names(cs._KC_SERVICE) == ["llm.cloud.openrouter", "search.brave"]
    assert api.enumerate_filters[-1] == "VOOL/credentials/*"
    assert not api.live


def test_the_2560_byte_blob_limit_is_refused_before_any_write():
    api = FakeAdvapi32()
    store = _store(api)
    store.set_password(cs._KC_SERVICE, "llm.cloud.generic", "a" * 2560)
    with pytest.raises(wcm.CredentialTooLarge) as caught:
        store.set_password(cs._KC_SERVICE, "llm.cloud.openrouter", "a" * 2561)
    assert "2561 bytes" in str(caught.value) and "2560" in str(caught.value)
    assert "aaaa" not in str(caught.value)
    # Multi-byte characters count as their UTF-8 bytes: 1281 x 2 bytes = 2562.
    with pytest.raises(wcm.CredentialTooLarge):
        store.set_password(cs._KC_SERVICE, "llm.cloud.openrouter", "é" * 1281)
    assert list(api.records) == ["VOOL/credentials/llm.cloud.generic"]


@pytest.mark.parametrize("name", ["", "bad*name", "a/b", "a\\b", "tab\tname"])
def test_names_that_could_escape_the_namespace_are_refused(name):
    api = FakeAdvapi32()
    with pytest.raises(ValueError):
        _store(api).set_password(cs._KC_SERVICE, name, FAKE_KEY)
    assert api.records == {}


def test_a_call_for_another_service_is_refused():
    with pytest.raises(ValueError):
        _store(FakeAdvapi32()).get_password("someone-else", "llm.cloud.openrouter")


# ------------------------------------------------------------------ backend selection


def test_windows_selects_credential_manager_by_default_without_a_keychain_grant(windows):
    assert cs.active_backend() == "credential_manager"
    assert cs.backend_label() == "Windows Credential Manager"
    assert "Windows Credential Manager" in cs.backend_sentence()
    assert cs.active_storage_class() == "os-user-sealed"


def test_off_windows_nothing_changes_and_advapi32_is_never_loaded(monkeypatch):
    def _boom():
        raise AssertionError("advapi32 loaded off Windows")

    monkeypatch.setattr(wcm, "_load_advapi32", _boom)
    monkeypatch.delenv("VOOL_CREDENTIAL_STORE", raising=False)
    monkeypatch.setattr(cs.sys, "platform", "linux")
    assert cs.active_backend() == "vault"
    assert cs.backend_sentence().startswith("Keys live in VOOL's encrypted local vault")
    cs.store_credential("llm.cloud.openrouter", FAKE_KEY, label="OpenRouter")
    assert cs.get_credential("llm.cloud.openrouter") == FAKE_KEY
    assert "llm.cloud.openrouter" in _vault_raw()


def test_vault_mode_on_windows_keeps_the_vault(windows, monkeypatch):
    monkeypatch.setenv("VOOL_CREDENTIAL_STORE", "vault")
    assert cs.active_backend() == "vault"
    cs.store_credential("llm.cloud.openrouter", FAKE_KEY)
    assert windows.records == {}
    assert cs.backend_sentence().startswith("Keys live in VOOL's encrypted local vault")


def test_credential_manager_unavailable_keeps_the_vault_and_says_so(monkeypatch, capsys):
    monkeypatch.setattr(wcm, "_load_advapi32", lambda: None)
    monkeypatch.delenv("VOOL_CREDENTIAL_STORE", raising=False)
    monkeypatch.setattr(cs.sys, "platform", "win32")
    _installed_home_attended(monkeypatch)
    assert cs.active_backend() == "vault"
    cs.store_credential("llm.cloud.openrouter", FAKE_KEY)
    cs.get_credential("llm.cloud.openrouter")
    assert cs.get_credential("llm.cloud.openrouter") == FAKE_KEY
    assert "llm.cloud.openrouter" in _vault_raw()
    sentence = cs.backend_sentence()
    assert sentence.startswith("Windows Credential Manager is not available here")
    assert "encrypted local vault" in sentence and "file permissions" in sentence
    err = capsys.readouterr().err
    assert err.count(cs.WINDOWS_FALLBACK_NOTICE) == 1
    assert FAKE_KEY not in err


def test_a_session_without_a_logon_session_is_reported_unavailable(windows):
    windows.fail_enumerate = wcm.ERROR_NO_SUCH_LOGON_SESSION
    assert cs.active_backend() == "vault"


# ------------------------------------------------------------------ the public API through Credential Manager


def test_store_get_has_list_delete_go_through_credential_manager(windows):
    cs.store_credential("llm.cloud.openrouter", FAKE_KEY, label="OpenRouter")
    assert windows.records["VOOL/credentials/llm.cloud.openrouter"]["blob"] == FAKE_KEY.encode()
    assert "llm.cloud.openrouter" not in _vault_raw()
    assert cs.get_credential("llm.cloud.openrouter") == FAKE_KEY
    assert cs.has_credential("llm.cloud.openrouter")
    assert cs.list_credentials() == [{"name": "llm.cloud.openrouter", "label": "OpenRouter"}]
    assert cs.delete_credential("llm.cloud.openrouter") is True
    assert windows.records == {}
    assert cs.get_credential("llm.cloud.openrouter") is None
    assert cs.delete_credential("llm.cloud.openrouter") is False
    assert not windows.live


def test_an_oversized_value_is_refused_in_plain_words_and_nothing_is_written(windows):
    from core.unattended_preflight import SecureStorageError

    with pytest.raises(SecureStorageError) as caught:
        cs.store_credential("llm.cloud.openrouter", "f" * 3000)
    assert caught.value.code == "value_too_large"
    assert "3000 bytes" in str(caught.value) and "fffff" not in str(caught.value)
    assert windows.records == {} and _vault_raw() == {}


def test_a_failed_write_is_typed_and_lands_nowhere(windows):
    from core.unattended_preflight import SecureStorageError

    windows.fail_write = 5
    with pytest.raises(SecureStorageError):
        cs.store_credential("llm.cloud.openrouter", FAKE_KEY)
    assert windows.records == {} and _vault_raw() == {}


def test_a_failed_read_is_unknown_under_strict_reads(windows):
    cs.store_credential("llm.cloud.openrouter", FAKE_KEY)
    windows.fail_read = 5
    assert cs.get_credential("llm.cloud.openrouter") is None
    with cs.strict_reads(), pytest.raises(cs.CredentialReadError):
        cs.get_credential("llm.cloud.openrouter")


def test_a_wiped_index_is_rebuilt_from_the_enumerate(windows):
    cs.store_credential("llm.cloud.openrouter", FAKE_KEY, label="OpenRouter")
    cs.store_credential("email.imap.work", OTHER_FAKE, label="Mine")
    runtime_paths.data_path("credentials.meta.json").unlink()
    names = [item["name"] for item in cs.list_credentials()]
    assert names == ["email.imap.work", "llm.cloud.openrouter"]


def test_diagnostics_name_credential_manager_and_never_carry_a_value(windows):
    cs.store_credential("llm.cloud.openrouter", FAKE_KEY, label="OpenRouter")
    diag = cs.export_diagnostics()
    assert diag["backend"] == "credential_manager"
    assert FAKE_KEY not in json.dumps(diag)
    assert diag["credentials"][0]["masked_suffix"] == "…1234"


# ------------------------------------------------------------------ migration vault -> Credential Manager


def _seed_vault(monkeypatch, entries: dict[str, str]) -> None:
    monkeypatch.setenv("VOOL_CREDENTIAL_STORE", "vault")
    for name, value in entries.items():
        cs.store_credential(name, value, label=name.split(".")[-1])
    monkeypatch.delenv("VOOL_CREDENTIAL_STORE", raising=False)


def test_migration_moves_each_key_once_after_a_verified_read_back(windows, monkeypatch):
    _seed_vault(monkeypatch, {"llm.cloud.openrouter": FAKE_KEY, "search.brave": OTHER_FAKE})
    assert set(_vault_raw()) == {"llm.cloud.openrouter", "search.brave"}

    assert cs.get_credential("llm.cloud.openrouter") == FAKE_KEY
    assert windows.records["VOOL/credentials/llm.cloud.openrouter"]["blob"] == FAKE_KEY.encode()
    assert windows.records["VOOL/credentials/search.brave"]["blob"] == OTHER_FAKE.encode()
    assert _vault_raw() == {}
    assert runtime_paths.data_path(cs._MIGRATION_MARKER).exists()
    assert {item["name"] for item in cs.list_credentials()} == {"llm.cloud.openrouter", "search.brave"}
    writes = len(windows.records)
    windows.records.clear()  # once: a later read does not migrate again
    cs.get_credential("search.brave")
    assert windows.records == {} and writes == 2


def test_a_failed_write_keeps_the_vault_entry_and_says_so(windows, monkeypatch, capsys):
    _seed_vault(monkeypatch, {"llm.cloud.openrouter": FAKE_KEY})
    windows.fail_write = 1312
    assert cs.get_credential("llm.cloud.openrouter") == FAKE_KEY  # still served from the vault
    assert cs.get_credential("llm.cloud.openrouter") == FAKE_KEY
    assert "llm.cloud.openrouter" in _vault_raw()
    assert not runtime_paths.data_path(cs._MIGRATION_MARKER).exists()
    err = capsys.readouterr().err
    assert err.count("could not be moved into Windows Credential Manager") == 1
    assert FAKE_KEY not in err


def test_a_read_back_that_does_not_match_keeps_the_vault_entry(windows, monkeypatch):
    _seed_vault(monkeypatch, {"llm.cloud.openrouter": FAKE_KEY})
    windows.corrupt_readback = True
    cs._maybe_migrate()
    assert "llm.cloud.openrouter" in _vault_raw()
    assert not runtime_paths.data_path(cs._MIGRATION_MARKER).exists()


def test_migration_retries_on_a_later_run_once_credential_manager_answers(windows, monkeypatch):
    _seed_vault(monkeypatch, {"llm.cloud.openrouter": FAKE_KEY})
    windows.fail_write = 5
    cs._maybe_migrate()
    assert "llm.cloud.openrouter" in _vault_raw()
    windows.fail_write = None
    cs._maybe_migrate()
    assert _vault_raw() == {}
    assert cs.get_credential("llm.cloud.openrouter") == FAKE_KEY


# ------------------------------------------------------------------ the vault file stays owner-only


@pytest.fixture
def sealing(monkeypatch):
    monkeypatch.setattr(owner_only, "_SEAL_SECRETS", True)
    monkeypatch.setattr(owner_only, "_REPAIRED", set())


def test_the_vault_refuses_to_write_when_owner_only_cannot_be_set(sealing, monkeypatch):
    monkeypatch.setenv("VOOL_CREDENTIAL_STORE", "vault")
    monkeypatch.setattr(
        owner_only, "make_owner_only", lambda path, directory=False: owner_only.OwnerOnlyReport(False, "no_acl_support")
    )
    with pytest.raises(owner_only.OwnerOnlyUnavailable):
        cs.store_credential("llm.cloud.openrouter", FAKE_KEY)
    folder = runtime_paths.data_path()
    assert not (folder / "credentials.enc.json").exists()
    assert not [p for p in folder.iterdir() if p.name.endswith(".tmp")]


def test_an_unknown_acl_state_fails_closed(sealing, monkeypatch):
    monkeypatch.setenv("VOOL_CREDENTIAL_STORE", "vault")

    def _unknown(path, directory=False):
        raise OSError("descriptor could not be read")

    monkeypatch.setattr(owner_only, "make_owner_only", _unknown)
    with pytest.raises(OSError):
        cs.store_credential("llm.cloud.openrouter", FAKE_KEY)
    assert not runtime_paths.data_path("credentials.enc.json").exists()


def test_a_vault_that_fails_its_after_write_check_is_refused(sealing, monkeypatch):
    monkeypatch.setenv("VOOL_CREDENTIAL_STORE", "vault")
    monkeypatch.setattr(owner_only, "verify_owner_only", lambda path: owner_only.OwnerOnlyReport(False, "extra_ace"))
    with pytest.raises(owner_only.OwnerOnlyUnavailable):
        cs.store_credential("llm.cloud.openrouter", FAKE_KEY)


def test_migration_with_an_unsealable_vault_keeps_both_copies_and_reads_on(windows, monkeypatch, sealing):
    _seed_vault(monkeypatch, {"llm.cloud.openrouter": FAKE_KEY})
    monkeypatch.setattr(
        owner_only, "make_owner_only", lambda path, directory=False: owner_only.OwnerOnlyReport(False, "no_acl_support")
    )
    assert cs.get_credential("llm.cloud.openrouter") == FAKE_KEY
    assert "llm.cloud.openrouter" in _vault_raw()  # not rewritten unprotected
    assert not runtime_paths.data_path(cs._MIGRATION_MARKER).exists()


# ------------------------------------------------------------------ every "where are my keys" surface


def test_chat_replies_name_credential_manager(windows, monkeypatch):
    from core.agent_runtime.fast_command_surface import (
        maybe_handle_cloud_diagnostics_intent,
        maybe_handle_key_location_intent,
    )

    monkeypatch.setattr("core.windows_path_policy._IS_WINDOWS", True)
    cs.store_credential("llm.cloud.openrouter", FAKE_KEY, label="OpenRouter")
    where = maybe_handle_key_location_intent("where are my keys stored?", owner_local=True)
    diag = maybe_handle_cloud_diagnostics_intent("cloud diagnostics", owner_local=True)
    for reply in (where, diag):
        assert reply is not None
        assert "Windows Credential Manager" in reply
        assert "Keychain" not in reply and "encrypted local vault" not in reply
        assert FAKE_KEY not in reply
    assert cs.backend_sentence() in where and cs.backend_sentence() in diag


def test_settings_credentials_list_carries_the_true_store(windows):
    from core.command_registry.groups.convergence import _handle_credentials_list

    cs.store_credential("llm.cloud.openrouter", FAKE_KEY, label="OpenRouter")
    data = _handle_credentials_list(None, None).data
    assert data["backend"] == "credential_manager"
    assert FAKE_KEY not in json.dumps(data)


def test_the_settings_page_words_match_the_store_sentences():
    from core.vool_settings_page import _SETTINGS_JS2 as _SETTINGS_JS

    for backend in ("credential_manager", "keychain", "vault"):
        sentence = cs.backend_sentence(backend) if backend != "vault" else (
            "Keys live in VOOL\\u2019s encrypted local vault in your user profile, protected by your "
            "account\\u2019s file permissions, not by the OS secret store"
        )
        assert f"{backend}: '{sentence}.'" in _SETTINGS_JS, backend


def test_backend_label_covers_every_backend():
    assert cs.backend_label("keychain") == "the macOS Keychain"
    assert cs.backend_label("credential_manager") == "Windows Credential Manager"
    assert cs.backend_label("vault") == "the encrypted local vault"


def test_a_save_whose_index_cannot_be_sealed_is_refused_and_leaves_nothing_behind(windows, sealing, monkeypatch):
    monkeypatch.setattr(
        owner_only, "make_owner_only", lambda path, directory=False: owner_only.OwnerOnlyReport(False, "no_acl_support")
    )
    with pytest.raises(owner_only.OwnerOnlyUnavailable):
        cs.store_credential("llm.cloud.openrouter", FAKE_KEY)
    assert windows.records == {}  # "the key wasn't saved" stays true


# ------------------------------------------------------------------ re-indexing enumerated names (security review B)


def _wipe_index() -> None:
    runtime_paths.data_path("credentials.meta.json").unlink(missing_ok=True)


@pytest.mark.parametrize(
    "planted",
    [
        "totally.unknown.thing",          # grammar-shaped, but no VOOL slot family
        "notvool",                        # one segment
        "llm.cloud.<img src=x onerror=1>",  # a family, but not the name grammar
        "llm.cloud.[click](https:x)",     # markdown-shaped
        "LLM.cloud.openrouter",           # case-folded look-alike of a known slot
    ],
)
def test_a_planted_unknown_name_is_never_indexed_and_never_logged(windows, capsys, planted):
    windows.plant("VOOL/credentials/" + planted, b"fake-planted")
    _wipe_index()
    assert cs.list_credentials() == []
    assert not cs.has_credential(planted)
    assert planted not in cs._meta_load()
    err = capsys.readouterr().err
    assert planted not in err
    assert "1 unrecognised" in err


def test_a_planted_known_slot_is_indexed_only_after_its_read_confirms_it(windows):
    windows.plant("VOOL/credentials/llm.cloud.openrouter", b"")  # present, but reads back empty
    _wipe_index()
    assert cs.list_credentials() == []
    windows.fail_read = 5  # present, but its read fails
    assert cs.list_credentials() == []
    assert "llm.cloud.openrouter" not in cs._meta_load()
    windows.fail_read = None
    windows.plant("VOOL/credentials/llm.cloud.openrouter", FAKE_KEY.encode())
    assert cs.list_credentials() == [{"name": "llm.cloud.openrouter", "label": "OpenRouter"}]


def test_a_family_name_in_the_write_grammar_is_reindexed_after_its_read(windows):
    windows.plant("VOOL/credentials/email.smtp.me@example.com", b"fake-app-password")
    _wipe_index()
    assert [item["name"] for item in cs.list_credentials()] == ["email.smtp.me@example.com"]


def test_the_windows_store_reports_the_os_user_sealed_class(windows):
    from core.secret_storage import (
        STORAGE_CLASS_DESCRIPTIONS,
        STORAGE_CLASS_KEYCHAIN,
        STORAGE_CLASS_OS_USER_SEALED,
    )

    assert STORAGE_CLASS_OS_USER_SEALED == "os-user-sealed"
    assert cs.active_storage_class() == STORAGE_CLASS_OS_USER_SEALED
    text = STORAGE_CLASS_DESCRIPTIONS[STORAGE_CLASS_OS_USER_SEALED].lower()
    assert "sign-in" in text and "any program running as that user" in text
    assert "windows" not in STORAGE_CLASS_DESCRIPTIONS[STORAGE_CLASS_KEYCHAIN].lower()
    cs.store_credential("llm.cloud.openrouter", FAKE_KEY)
    assert cs.export_diagnostics()["storage_class"] == "os-user-sealed"


# ------------------------------------------------------------------ scratch, unattended and isolated homes


def _no_advapi32(monkeypatch) -> None:
    def _boom():
        raise AssertionError("advapi32 loaded for a process that must keep to the vault")

    monkeypatch.setattr(wcm, "_load_advapi32", _boom)
    monkeypatch.delenv("VOOL_CREDENTIAL_STORE", raising=False)
    monkeypatch.setattr(cs.sys, "platform", "win32")


def test_a_scratch_process_on_windows_keeps_to_the_vault_and_never_loads_advapi32(monkeypatch):
    """Credential Manager is the user's global store, outside VOOL's home: a test or scratch
    process (this pytest run, on a tmp_path home) must leave nothing there."""
    _no_advapi32(monkeypatch)
    assert cs.active_backend() == "vault"
    cs.store_credential("llm.cloud.openrouter", FAKE_KEY, label="OpenRouter")
    assert cs.get_credential("llm.cloud.openrouter") == FAKE_KEY
    assert cs.has_credential("llm.cloud.openrouter")
    assert [row["name"] for row in cs.list_credentials()] == ["llm.cloud.openrouter"]
    assert "llm.cloud.openrouter" in _vault_raw()
    assert cs.active_backend() == "vault"


@pytest.mark.parametrize(("unattended", "isolated"), [(False, True), (True, False), (True, True)])
def test_only_an_attended_run_on_the_installed_home_uses_credential_manager_without_a_grant(
    monkeypatch, unattended, isolated
):
    import core.unattended_preflight as preflight

    monkeypatch.setattr(preflight, "is_unattended", lambda: unattended)
    monkeypatch.setattr(runtime_paths, "runtime_home_is_isolated", lambda env=None: isolated)
    _no_advapi32(monkeypatch)
    assert cs.active_backend() == "vault"
    cs.store_credential("llm.cloud.openrouter", FAKE_KEY)
    assert "llm.cloud.openrouter" in _vault_raw()


def test_an_attended_run_on_the_installed_home_uses_credential_manager(monkeypatch, api):
    import core.unattended_preflight as preflight

    monkeypatch.setattr(preflight, "is_unattended", lambda: False)
    monkeypatch.setattr(runtime_paths, "runtime_home_is_isolated", lambda env=None: False)
    monkeypatch.delenv("VOOL_CREDENTIAL_STORE", raising=False)
    monkeypatch.setattr(cs.sys, "platform", "win32")
    assert cs.active_backend() == "credential_manager"
    cs.store_credential("llm.cloud.openrouter", FAKE_KEY)
    assert "llm.cloud.openrouter" not in _vault_raw()
    assert any(name.endswith("llm.cloud.openrouter") for name in api.records)


def test_an_unattended_run_reads_a_key_already_in_credential_manager_but_never_writes(monkeypatch, api):
    """The macOS read rule: a home that used the OS store before still finds its keys there in a
    background run; storing still needs the attended app or a grant, as on macOS."""
    monkeypatch.delenv("VOOL_CREDENTIAL_STORE", raising=False)
    monkeypatch.setattr(cs.sys, "platform", "win32")
    monkeypatch.setattr(cs, "_windows_home_may_use_credential_manager", lambda: True)
    cs.store_credential("llm.cloud.openrouter", FAKE_KEY)
    written = dict(api.records)
    assert written and "llm.cloud.openrouter" not in _vault_raw()
    monkeypatch.setattr(cs, "_windows_home_may_use_credential_manager", lambda: False)
    assert cs.get_credential("llm.cloud.openrouter") == FAKE_KEY
    assert cs._active_keyring(write=True) is None
    assert api.records == written
