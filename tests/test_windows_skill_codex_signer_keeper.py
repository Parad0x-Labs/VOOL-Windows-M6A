"""Windows: the Skill Codex signing authority keeps its keys in Credential Manager and its state in one folder.

Owner decisions D15-D17 and D19 (16 Nov 2026, Security Review's design bar). Rules covered here:
- The four secrets are generic credentials under VOOL/registry-authority/, persisted on this computer only.
  The main key is held only as passphrase-encrypted PKCS8, and nothing but setup makes a key.
- Missing or unreadable keys refuse, as does Credential Manager being unavailable. There is no vault fallback.
- --dir must be exactly the fixed folder under the shell's LocalAppData. A link on the way refuses, and so
  does a folder or file that isn't owner-only.
- VOOL's own credential surfaces never see or touch the authority's namespace.
- The authority is a command-line tool: nothing in the app imports it.

The Windows branch is driven on any platform: mac_authority._IS_WINDOWS, the shell's known folder, and a
fake advapi32 (the conftest guard keeps the real one out of every test).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from core import credential_store as cs
from core import owner_only, runtime_paths, windows_known_folders
from core import windows_credential_manager as wcm
from core.registry.codec import RegistryError
from registry_service import mac_authority, windows_keeper
from tests.registry.helpers import TARGET
from tests.test_windows_credential_manager_store import FakeAdvapi32

REPO = Path(__file__).resolve().parents[1]
PASSPHRASE = "correct horse battery"
NAMES = ("admission", "review", "root", "totp")


@pytest.fixture
def api(monkeypatch):
    fake = FakeAdvapi32()
    monkeypatch.setattr(wcm, "_load_advapi32", lambda: fake)
    return fake


@pytest.fixture
def windows(tmp_path, monkeypatch, api):
    """This process is Windows for the signer, the shell's LocalAppData is tmp_path/LocalAppData."""
    local = tmp_path / "LocalAppData"
    local.mkdir()
    monkeypatch.setattr(mac_authority, "_IS_WINDOWS", True)
    monkeypatch.setattr(windows_known_folders, "_IS_WINDOWS", True)
    monkeypatch.setattr(windows_known_folders, "_shell_known_folder", lambda name: str(local) if name == "LocalAppData" else None)
    monkeypatch.setattr(windows_known_folders, "_full_local_path", lambda value: True)
    return api


def _folder() -> Path:
    return windows_keeper.authority_folder()


def _setup(directory: Path | None = None):
    return mac_authority.setup(directory or _folder(), server="https://codex.example", catalog_key="ab" * 32,
                               targets=[TARGET], review={"provider": "ollama", "model": "m"}, passphrase=PASSPHRASE,
                               now=1_800_000_000)


def _targets(api) -> list[str]:
    return sorted(target for target in api.records if target.startswith("VOOL/registry-authority/"))


def test_setup_puts_every_key_in_credential_manager_and_none_on_disk(windows) -> None:
    _setup()

    assert _targets(windows) == ["VOOL/registry-authority/" + name for name in NAMES]
    for target in _targets(windows):
        record = windows.records[target]
        assert record["persist"] == wcm.CRED_PERSIST_LOCAL_MACHINE and record["type"] == wcm.CRED_TYPE_GENERIC
        assert record["comment"] == "VOOL Skill Codex signing key"
    root = windows.records["VOOL/registry-authority/root"]["blob"]
    assert root.startswith(b"-----BEGIN ENCRYPTED PRIVATE KEY-----")
    for name in ("admission", "review"):
        assert len(bytes.fromhex(windows.records["VOOL/registry-authority/" + name]["blob"].decode())) == 32
    assert len(bytes.fromhex(windows.records["VOOL/registry-authority/totp"]["blob"].decode())) == 20
    kept = sorted(path.name for path in _folder().iterdir())
    assert kept == ["config.json", "state.json", "trusted-root.json"]
    assert not any(name in kept for name in windows_keeper.KEYS)


def test_the_authority_opens_signs_gates_and_unlocks_from_credential_manager(windows) -> None:
    _uri, secret = _setup()
    authority = mac_authority.Authority(_folder(), clock=lambda: 1_800_000_000)
    code = mac_authority.totp(secret, 1_800_000_000 // mac_authority.STEP)

    authority.gate(code)

    assert authority.state["totp_last"] == 1_800_000_000 // mac_authority.STEP
    with pytest.raises(RegistryError, match="already used"):
        authority.gate(code)
    assert mac_authority.unlock_root(_folder(), PASSPHRASE).public_key() is not None
    with pytest.raises(ValueError):
        mac_authority.unlock_root(_folder(), "not the passphrase")


def test_the_lock_needs_no_flock_on_windows(windows, monkeypatch) -> None:
    _setup()
    monkeypatch.setattr(mac_authority, "fcntl", None)
    authority = mac_authority.Authority(_folder(), clock=lambda: 1_800_000_000)

    with authority._locked():
        assert (_folder() / "lock").is_file()


def test_setup_refuses_when_any_authority_key_already_exists(windows) -> None:
    windows.plant("VOOL/registry-authority/review", b"00" * 32)

    with pytest.raises(RegistryError, match="keys are made once"):
        _setup()

    assert _targets(windows) == ["VOOL/registry-authority/review"]


def test_a_refused_setup_leaves_no_folder_behind(windows, monkeypatch) -> None:
    """Credential Manager is judged before the folder is made, so a refused setup can be run again."""
    windows.plant("VOOL/registry-authority/totp", b"00" * 20)
    with pytest.raises(RegistryError, match="keys are made once"):
        _setup()
    assert not _folder().exists()
    del windows.records["VOOL/registry-authority/totp"]
    monkeypatch.setattr(wcm, "_load_advapi32", lambda: None)
    with pytest.raises(RegistryError, match="Credential Manager isn't available"):
        _setup()
    assert not _folder().exists()


def test_a_key_write_that_fails_partway_leaves_no_folder_and_setup_can_run_again(windows, monkeypatch) -> None:
    real = windows.CredWriteW
    writes = []

    def second_fails(pcred, flags):
        writes.append(1)
        if len(writes) == 2:
            windows.error = 5  # ERROR_ACCESS_DENIED
            return 0
        return real(pcred, flags)

    monkeypatch.setattr(windows, "CredWriteW", second_fails)
    with pytest.raises(RegistryError, match="could not be saved in Windows Credential Manager"):
        _setup()
    assert _targets(windows) == [] and not _folder().exists()
    monkeypatch.setattr(windows, "CredWriteW", real)
    _setup()
    assert _targets(windows) == ["VOOL/registry-authority/" + name for name in NAMES]


def test_a_setup_that_fails_after_the_keys_are_saved_names_what_to_delete(windows, monkeypatch) -> None:
    real = windows_keeper.write

    def fail_on_state(path, data):
        if Path(path).name == "state.json":
            raise RegistryError("state.json could not be made private (unsupported), so it wasn't saved")
        return real(path, data)

    monkeypatch.setattr(windows_keeper, "write", fail_on_state)
    with pytest.raises(RegistryError, match=r"VOOL/registry-authority/\* in Windows Credential Manager"):
        _setup()


def test_the_store_folder_for_kept_skills_is_made_owner_only(windows, monkeypatch) -> None:
    _setup()
    made = []
    real = owner_only.ensure_private_dir
    monkeypatch.setattr(owner_only, "ensure_private_dir",
                        lambda path, fresh=False: made.append(Path(path).name) or real(path, fresh=fresh))
    authority = mac_authority.Authority(_folder(), clock=lambda: 1_800_000_000)

    authority._store(b'{"kept": true}')

    assert "artifacts" in made and owner_only.verify_owner_only(_folder() / "artifacts").ok


def test_setup_and_open_refuse_any_folder_but_the_fixed_one(windows, tmp_path) -> None:
    other = tmp_path / "elsewhere" / "authority"

    with pytest.raises(RegistryError, match="must be"):
        _setup(other)
    assert not other.exists() and _targets(windows) == []
    _setup()
    with pytest.raises(RegistryError, match="must be"):
        mac_authority.Authority(other)


def test_the_folder_is_its_own_sibling_of_vools_home_under_local_app_data(windows, tmp_path) -> None:
    """Owner decision D17: %LOCALAPPDATA%\\VOOL-Registry-Authority, outside VOOL's own home folder."""
    assert _folder() == tmp_path / "LocalAppData" / "VOOL-Registry-Authority"
    _setup()
    assert not (tmp_path / "LocalAppData" / "VOOL").exists()


def test_the_fixed_folder_reached_through_a_link_is_refused(windows, tmp_path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    _folder().symlink_to(real, target_is_directory=True)

    with pytest.raises(RegistryError, match="link"):
        _setup()
    assert _targets(windows) == [] and list(real.iterdir()) == []


def test_credential_manager_unavailable_refuses_with_no_fallback(windows, monkeypatch) -> None:
    monkeypatch.setattr(wcm, "_load_advapi32", lambda: None)

    with pytest.raises(RegistryError, match="Credential Manager isn't available"):
        _setup()


def test_a_missing_or_malformed_key_refuses_by_name(windows) -> None:
    _setup()
    del windows.records["VOOL/registry-authority/admission"]
    with pytest.raises(RegistryError, match="admission key is missing"):
        mac_authority.Authority(_folder())
    windows.plant("VOOL/registry-authority/admission", b"not hex")
    with pytest.raises(RegistryError, match=r"admission key .* is not one VOOL wrote"):
        mac_authority.Authority(_folder())


def test_an_unreadable_key_refuses(windows) -> None:
    _setup()
    windows.fail_read = 5  # ERROR_ACCESS_DENIED
    with pytest.raises(RegistryError, match="could not be read from Windows Credential Manager"):
        mac_authority.Authority(_folder())


def test_a_folder_or_file_that_is_not_owner_only_refuses(windows, monkeypatch) -> None:
    _setup()
    real = owner_only.verify_owner_only
    monkeypatch.setattr(owner_only, "verify_owner_only",
                        lambda path: owner_only.OwnerOnlyReport(False, "foreign_ace") if Path(path).name == "state.json"
                        else real(path))
    with pytest.raises(RegistryError, match=r"state\.json is missing or readable by others"):
        mac_authority.Authority(_folder())


def test_forget_root_removes_only_the_main_key(windows) -> None:
    _setup()

    assert windows_keeper.forget_root().startswith("Done.")

    assert _targets(windows) == ["VOOL/registry-authority/" + name for name in ("admission", "review", "totp")]
    mac_authority.Authority(_folder())  # daily signing never needs the main key
    assert windows_keeper.forget_root() == "This PC holds no main key."


# ---- VOOL's own credential surfaces never see the authority's namespace (one row per surface) ----

@pytest.fixture
def app_store(tmp_path, monkeypatch, windows):
    monkeypatch.setenv("VOOL_HOME", str(tmp_path / "home"))
    runtime_paths.configure_runtime_home(tmp_path / "home")
    monkeypatch.delenv("VOOL_CREDENTIAL_STORE", raising=False)
    monkeypatch.setattr(cs.sys, "platform", "win32")
    monkeypatch.setattr(cs, "_windows_home_may_use_credential_manager", lambda: True)
    _setup()
    planted = dict(windows.records)
    yield windows, planted
    runtime_paths.configure_runtime_home(None)


def test_the_settings_list_never_shows_authority_keys(app_store) -> None:
    api, _planted = app_store
    assert cs.list_credentials() == []
    assert all(not filt.startswith("VOOL/registry-authority") for filt in api.enumerate_filters if filt.startswith("VOOL/c"))


def test_a_wiped_index_is_rebuilt_without_authority_keys(app_store) -> None:
    cs.store_credential("llm.cloud.openrouter", "sk-or-v1-fake", label="OpenRouter")
    runtime_paths.data_path("credentials.meta.json").unlink()
    assert [item["name"] for item in cs.list_credentials()] == ["llm.cloud.openrouter"]


@pytest.mark.parametrize("name", NAMES)
def test_by_name_reads_and_deletes_never_reach_authority_keys(app_store, name) -> None:
    api, planted = app_store
    assert cs.get_credential(name) is None
    assert not cs.has_credential(name)
    assert cs.delete_credential(name) is False
    assert {t: r for t, r in api.records.items() if t.startswith("VOOL/registry-authority/")} == planted


@pytest.mark.parametrize("name", NAMES)
def test_a_by_name_write_lands_in_vools_own_namespace(app_store, name) -> None:
    api, planted = app_store
    cs.store_credential(name, "value", label="x")
    assert api.records["VOOL/credentials/" + name]["blob"] == b"value"
    assert {t: r for t, r in api.records.items() if t.startswith("VOOL/registry-authority/")} == planted


def test_deleting_every_listed_credential_leaves_the_authority_keys(app_store) -> None:
    """VOOL has no separate wipe: clearing the vault is a by-name delete of every listed entry."""
    api, planted = app_store
    cs.store_credential("llm.cloud.openrouter", "sk-or-v1-fake", label="OpenRouter")
    for item in cs.list_credentials():
        assert cs.delete_credential(item["name"])
    assert cs.list_credentials() == []
    assert {t: r for t, r in api.records.items() if t.startswith("VOOL/registry-authority/")} == planted


def test_forget_root_off_windows_refuses_before_spending_an_authenticator_code(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mac_authority, "_IS_WINDOWS", False)
    gated = []
    monkeypatch.setattr(mac_authority.Authority, "__init__", lambda self, directory, **kw: None)
    monkeypatch.setattr(mac_authority.Authority, "gate", lambda self, code: gated.append(code))
    monkeypatch.setattr(mac_authority.getpass, "getpass", lambda prompt="": "123456")

    assert mac_authority.main(["forget-root", "--dir", str(tmp_path / "authority")]) != 0
    assert gated == []


def test_only_the_signer_names_the_authority_namespace() -> None:
    hits = []
    for folder in ("apps", "core", "windows", "installer", "plugins", "skills"):
        for path in (REPO / folder).rglob("*.py"):
            if "registry-authority" in path.read_text(encoding="utf-8", errors="replace"):
                hits.append(path.relative_to(REPO).as_posix())
    assert hits == []


def test_nothing_in_the_app_imports_the_authority() -> None:
    """CLI-only: no app module, endpoint or tool can reach the signer or its keys."""
    signer = {"registry_service.mac_authority", "registry_service.windows_keeper", "registry_service.github_authority"}
    hits = []
    for folder in ("apps", "core", "windows", "installer", "plugins", "skills"):
        for path in (REPO / folder).rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                named = ([alias.name for alias in node.names] if isinstance(node, ast.Import) else
                         [node.module or ""] + [f"{node.module}.{alias.name}" for alias in node.names]
                         if isinstance(node, ast.ImportFrom) else [])
                if any(name in signer for name in named):
                    hits.append(path.relative_to(REPO).as_posix())
    assert hits == []
