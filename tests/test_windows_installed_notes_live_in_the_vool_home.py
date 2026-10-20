"""The installed Windows app keeps operator notes in the VOOL home, which uninstall keeps.

The bundle's launcher sets VOOL_HOME and VOOL_BUNDLE_ROOT but not VOOL_PROJECT_ROOT, and the
supervisor starts the API with the working directory {app}\\app. A Windows home is not an isolated
one, so resolve_workspace_root fell back to that working directory and every note landed in
{app}\\app\\notes, which vool.iss [UninstallDelete] removes with the program files. The macOS app
and a source checkout already keep notes in <home>\\workspace\\notes.

Only notes move (core.operator.notes._workspace_root). The unbound chat's default workspace and
resolve_workspace_root stay where they were in every layout, and the supervisor copies notes an
earlier build left in the program folder once, without deleting them.

The layout tests run the real modules in a child Python with the environment each launcher builds:
the installed bundle (VOOL.cmd's variables, then the supervisor's own child environment), a source
checkout (Start_VOOL.bat) and a direct developer run. The code root stands in for {app}\\app.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

from installer.bundle import bundle_supervisor
from installer.bundle.bundle_supervisor import BundleSupervisor

REPO = Path(__file__).resolve().parent.parent
ISS = REPO / "installer" / "bundle" / "vool.iss"

_PROBE = r"""
import json, sys
from core.runtime_paths import resolve_workspace_root
from core.web.api.runtime import default_workspace_root
from core.operator import notes

made = notes.create_note(title="Groceries", body="milk and eggs", session_id="probe")
back = notes.read_note(title="Groceries") or {}
archived = notes.archive_note(title="Groceries")
archived_path = next((str(p) for p in (notes.notes_root() / ".archive").glob("groceries-*.md")), "")
restored = notes.restore_note(title="Groceries")
print(json.dumps({
    "note_path": made["note_path"],
    "notes_root": str(notes.notes_root()),
    "read_back": back.get("body", ""),
    "archived_ok": bool(archived.get("ok")),
    "archived_path": archived_path,
    "restored_ok": bool(restored.get("ok")),
    "restored_path": str(restored.get("note_path") or ""),
    "workspace_root": str(resolve_workspace_root()),
    "default_workspace_root": str(default_workspace_root()),
}))
"""


def _base_env(tmp_path: Path) -> dict[str, str]:
    user = tmp_path / "user"
    (user / "Desktop").mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(user),
        "USERPROFILE": str(user),
        "LOCALAPPDATA": str(tmp_path / "localappdata"),
        "PYTHONPATH": str(REPO),
        "PYTHONUTF8": "1",
        "VOOL_PLUGINS_DIR": str(tmp_path / "no-plugins"),
        "VOOL_BUNDLED_PLUGINS_DIR": str(tmp_path / "no-bundled-plugins"),
    }
    for name in ("SYSTEMROOT", "SystemRoot", "TEMP", "TMP", "TMPDIR", "COMSPEC"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    return env


def _probe(cwd: Path, env: dict[str, str]) -> dict[str, str]:
    done = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", _PROBE],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert done.returncode == 0, done.stderr[-4000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def _install(tmp_path: Path) -> Path:
    app = tmp_path / "Programs" / "VOOL D’Angelo [x]"
    (app / "app").mkdir(parents=True)
    (app / "bundle_manifest.json").write_text(
        json.dumps({"schema": "vool.bundle_manifest.v1", "selected_model": "qwen2.5:7b"}) + "\n", encoding="utf-8"
    )
    return app


def _installed_api_env(tmp_path: Path, app: Path, home: Path) -> dict[str, str]:
    # VOOL.cmd: VOOL_BUNDLE_ROOT is %~dp0 (with its trailing separator) and VOOL_HOME the native home.
    launcher_env = {**_base_env(tmp_path), "VOOL_BUNDLE_ROOT": str(app) + os.sep, "VOOL_HOME": str(home)}
    supervisor = BundleSupervisor(app, env=launcher_env)
    supervisor._prepare()
    env = supervisor._child_env()
    # The child's PYTHONPATH starts with {app}\app; the code root stands in for it here.
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env["PYTHONPATH"]
    return env


def _native_home(tmp_path: Path) -> Path:
    """The home the installed app runs with, which counts as a default (not isolated) home: what
    native_runtime_home gives on Windows, and its counterpart on the Linux CI shards."""
    if sys.platform == "win32":
        return tmp_path / "localappdata" / "VOOL"
    return tmp_path / "user" / ".vool_runtime"


def _resolved(path: Path) -> str:
    return str(path.resolve())


def test_the_installed_app_keeps_notes_in_the_vool_home_and_the_unbound_default_where_it_was(tmp_path: Path) -> None:
    app, home = _install(tmp_path), _native_home(tmp_path)
    before = set((REPO / "notes").glob("groceries-*.md"))

    try:
        seen = _probe(REPO, _installed_api_env(tmp_path, app, home))
    finally:
        # A regression writes the probe's note into the code root (the bug this file covers):
        # remove it so a failed run leaves the checkout as it found it.
        for leftover in set((REPO / "notes").glob("groceries-*.md")) - before:
            leftover.unlink()

    notes_dir = home / "workspace" / "notes"
    assert seen["notes_root"] == _resolved(notes_dir)
    assert Path(seen["note_path"]).parent == notes_dir.resolve()
    assert seen["read_back"] == "milk and eggs"
    # The archive and restore (notes/.archive) follow the same root.
    assert seen["archived_ok"] and seen["restored_ok"], seen
    assert Path(seen["archived_path"]).parent == (notes_dir / ".archive").resolve()
    assert Path(seen["restored_path"]).parent == notes_dir.resolve() and Path(seen["restored_path"]).is_file()
    assert set((REPO / "notes").glob("groceries-*.md")) == before
    # Unchanged: the API's working directory ({app}\app) is still the workspace and the unbound default.
    assert seen["workspace_root"] == _resolved(REPO)
    assert seen["default_workspace_root"] == _resolved(REPO)


def test_a_source_checkout_keeps_its_notes_and_unbound_default_unchanged(tmp_path: Path) -> None:
    home = tmp_path / "checkout-home"
    # Start_VOOL.bat: PYTHONPATH and VOOL_PROJECT_ROOT are the checkout, the API runs from it.
    env = {**_base_env(tmp_path), "VOOL_PROJECT_ROOT": str(REPO), "VOOL_HOME": str(home)}

    seen = _probe(REPO, env)

    assert seen["notes_root"] == _resolved(home / "workspace" / "notes")
    assert seen["workspace_root"] == _resolved(home / "workspace")
    assert seen["default_workspace_root"] == str(tmp_path / "user" / "Desktop")


def test_a_direct_developer_run_keeps_notes_in_its_working_directory(tmp_path: Path) -> None:
    home, project = tmp_path / "dev-home", tmp_path / "my project"
    project.mkdir()

    seen = _probe(project, {**_base_env(tmp_path), "VOOL_HOME": str(home)})

    assert seen["notes_root"] == _resolved(project / "notes")
    assert seen["workspace_root"] == _resolved(project)
    assert seen["default_workspace_root"] == _resolved(project)


def test_an_explicit_workspace_root_still_wins_in_the_installed_app(tmp_path: Path) -> None:
    app, home, chosen = _install(tmp_path), _native_home(tmp_path), tmp_path / "chosen"
    chosen.mkdir()
    env = _installed_api_env(tmp_path, app, home)
    env["VOOL_WORKSPACE_ROOT"] = str(chosen)

    seen = _probe(REPO, env)

    assert seen["notes_root"] == _resolved(chosen / "notes")
    assert seen["default_workspace_root"] == _resolved(chosen)


def test_the_supervisor_copies_old_program_folder_notes_once_and_keeps_the_originals(tmp_path: Path) -> None:
    app, home = _install(tmp_path), _native_home(tmp_path)
    old = app / "app" / "notes"
    (old / ".archive").mkdir(parents=True)
    (old / "plan-1a2b3c4d.md").write_text("---\ntitle: Plan\n---\n\nship it\n", encoding="utf-8")
    (old / ".archive" / "old-9f.md").write_text("archived\n", encoding="utf-8")
    new = home / "workspace" / "notes"
    new.mkdir(parents=True)
    (new / "plan-1a2b3c4d.md").write_text("newer copy\n", encoding="utf-8")
    (old / "todo.md").write_text("call bank\n", encoding="utf-8")

    env = {**_base_env(tmp_path), "VOOL_BUNDLE_ROOT": str(app), "VOOL_HOME": str(home)}
    BundleSupervisor(app, env=env)._prepare()

    assert (new / "todo.md").read_text(encoding="utf-8") == "call bank\n"
    assert (new / ".archive" / "old-9f.md").read_text(encoding="utf-8") == "archived\n"
    assert (new / "plan-1a2b3c4d.md").read_text(encoding="utf-8") == "newer copy\n", "an existing note is never replaced"
    for name in ("todo.md", "plan-1a2b3c4d.md", ".archive/old-9f.md"):
        assert (old / name).is_file(), f"the original {name} must stay"
    marker = json.loads((home / "run" / bundle_supervisor._NOTES_COPY_MARKER).read_text(encoding="utf-8"))
    assert sorted(marker["copied"]) == sorted(["todo.md", str(Path(".archive") / "old-9f.md")])
    assert marker["kept"] == ["plan-1a2b3c4d.md"]

    # Once: a note deleted in the new place is not brought back by the next start.
    (new / "todo.md").unlink()
    BundleSupervisor(app, env=env)._prepare()
    assert not (new / "todo.md").exists()


def test_the_copy_lands_where_the_installed_api_reads_its_notes(tmp_path: Path) -> None:
    app, home = _install(tmp_path), _native_home(tmp_path)
    old = app / "app" / "notes"
    old.mkdir(parents=True)
    (old / "milk-0a0a0a0a.md").write_text("---\ntitle: Milk\n---\n\nbuy oat milk\n", encoding="utf-8")

    env = _installed_api_env(tmp_path, app, home)
    done = subprocess.run(
        [sys.executable, "-X", "utf8", "-c",
         "import json; from core.operator import notes; print(json.dumps(notes.read_note(title='Milk')))"],
        cwd=str(REPO), env=env, capture_output=True, text=True, timeout=180,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert done.returncode == 0, done.stderr[-4000:]
    found = json.loads(done.stdout.strip().splitlines()[-1])
    assert found and found["body"] == "buy oat milk"
    assert Path(found["note_path"]).parent == (home / "workspace" / "notes").resolve()


def test_a_failed_copy_leaves_no_marker_and_no_half_written_note(tmp_path: Path, monkeypatch) -> None:
    app, home = _install(tmp_path), _native_home(tmp_path)
    old = app / "app" / "notes"
    old.mkdir(parents=True)
    (old / "a.md").write_text("a\n", encoding="utf-8")
    real_open = Path.open

    def failing_open(self, mode="r", *args, **kwargs):
        if mode == "rb" and self.name == "a.md":
            raise OSError("disk read failed")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_open)
    env = {**_base_env(tmp_path), "VOOL_BUNDLE_ROOT": str(app), "VOOL_HOME": str(home)}
    BundleSupervisor(app, env=env)._prepare()
    monkeypatch.setattr(Path, "open", real_open)

    assert not (home / "run" / bundle_supervisor._NOTES_COPY_MARKER).exists()
    assert not (home / "workspace" / "notes" / "a.md").exists()
    BundleSupervisor(app, env=env)._prepare()
    assert (home / "workspace" / "notes" / "a.md").read_text(encoding="utf-8") == "a\n"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_the_copy_never_follows_a_link_out_of_the_old_notes_folder(tmp_path: Path) -> None:
    app, home = _install(tmp_path), _native_home(tmp_path)
    old, secrets = app / "app" / "notes", tmp_path / "elsewhere"
    old.mkdir(parents=True)
    secrets.mkdir()
    (secrets / "private.md").write_text("not a note\n", encoding="utf-8")
    (old / "real.md").write_text("real\n", encoding="utf-8")
    try:
        os.symlink(secrets, old / "linked-folder", target_is_directory=True)
        os.symlink(secrets / "private.md", old / "linked.md")
    except OSError as exc:  # Windows without the symlink privilege
        pytest.skip(f"cannot create symlinks here: {exc}")

    env = {**_base_env(tmp_path), "VOOL_BUNDLE_ROOT": str(app), "VOOL_HOME": str(home)}
    BundleSupervisor(app, env=env)._prepare()

    new = home / "workspace" / "notes"
    assert (new / "real.md").is_file()
    assert not (new / "linked.md").exists() and not (new / "linked-folder").exists()


# ----------------------------------------------------------------------------- uninstall keeps it


def _sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current = ""
    for raw in text.splitlines():
        line = raw.strip()
        header = re.fullmatch(r"\[(\w+)\]", line)
        if header:
            current = header.group(1)
            continue
        if line and not line.startswith(";"):
            sections.setdefault(current, []).append(line)
    return sections


def _pascal_code_without_comments(lines: list[str]) -> str:
    code = "\n".join(lines)
    code = re.sub(r"\{.*?\}", " ", code, flags=re.DOTALL)  # { block comments }
    code = re.sub(r"\(\*.*?\*\)", " ", code, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", " ", code)


def test_the_uninstaller_never_names_the_vool_home() -> None:
    sections = _sections(ISS.read_text(encoding="utf-8"))
    for name in ("UninstallDelete", "UninstallRun", "Dirs", "InstallDelete"):
        for entry in sections.get(name, []):
            assert "{localappdata}" not in entry.lower() and "localappdata" not in entry.lower(), f"[{name}] {entry}"
    code = _pascal_code_without_comments(sections.get("Code", [])).lower()
    for needle in ("localappdata", "deltree", "deletefile", "removedir", "remove-item"):
        assert needle not in code, f"[Code] mentions {needle}"
    # The only [Files] entry that writes the VOOL home is never uninstalled.
    for entry in sections.get("Files", []):
        if "{localappdata}" in entry.lower():
            assert "uninsneveruninstall" in entry, entry


def test_the_installed_notes_folder_is_outside_everything_uninstall_deletes(tmp_path: Path) -> None:
    sections = _sections(ISS.read_text(encoding="utf-8"))
    deleted = [re.search(r'Name:\s*"([^"]+)"', e).group(1) for e in sections.get("UninstallDelete", [])]
    app, home = tmp_path / "Programs" / "VOOL", tmp_path / "localappdata" / "VOOL"
    expanded = [Path(name.replace("{app}", str(app)).replace("\\", os.sep)) for name in deleted]
    notes = home / "workspace" / "notes"
    for root in expanded:
        assert root != notes and root not in notes.parents, f"uninstall deletes {root}, which holds {notes}"


def test_the_stop_module_leaves_the_vool_home_notes_alone(tmp_path: Path) -> None:
    from installer.vool_stop import hard_stop

    app, home = tmp_path / "Programs" / "VOOL" / "app", tmp_path / "localappdata" / "VOOL"
    app.mkdir(parents=True)
    notes = home / "workspace" / "notes"
    notes.mkdir(parents=True)
    (notes / "keep.md").write_text("keep\n", encoding="utf-8")

    def run(cmd, capture_output=False, text=False, check=False):
        return types.SimpleNamespace(stdout="", returncode=0)

    hard_stop(app, home, run=run)

    assert (notes / "keep.md").read_text(encoding="utf-8") == "keep\n"
    assert sorted(p.relative_to(home).as_posix() for p in home.rglob("*") if p.is_file()) == [
        "run/stop", "workspace/notes/keep.md"
    ]


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows filesystem feature")
def test_the_copy_never_follows_a_junction_out_of_the_old_notes_folder(tmp_path: Path) -> None:
    import _winapi

    app, home = _install(tmp_path), _native_home(tmp_path)
    old, elsewhere = app / "app" / "notes", tmp_path / "elsewhere"
    old.mkdir(parents=True)
    elsewhere.mkdir()
    (elsewhere / "private.md").write_text("not a note\n", encoding="utf-8")
    (old / "real.md").write_text("real\n", encoding="utf-8")
    _winapi.CreateJunction(str(elsewhere), str(old / "junctioned"))  # needs no symlink privilege

    env = {**_base_env(tmp_path), "VOOL_BUNDLE_ROOT": str(app), "VOOL_HOME": str(home)}
    BundleSupervisor(app, env=env)._prepare()

    new = home / "workspace" / "notes"
    assert (new / "real.md").is_file()
    assert not (new / "junctioned").exists()
    assert (elsewhere / "private.md").read_text(encoding="utf-8") == "not a note\n"


@pytest.mark.parametrize(
    "override",
    [{"VOOL_WORKSPACE_ROOT": "chosen"}, {"VOOL_WORKSPACE_ROOT": "   ", "NULLA_WORKSPACE_ROOT": "chosen"}],
    ids=["vool", "blank-vool-then-legacy-nulla"],
)
def test_with_a_chosen_workspace_the_old_notes_are_not_copied_into_it(tmp_path: Path, override) -> None:
    app, home, chosen = _install(tmp_path), _native_home(tmp_path), tmp_path / "chosen"
    chosen.mkdir()
    old = app / "app" / "notes"
    old.mkdir(parents=True)
    (old / "a.md").write_text("a\n", encoding="utf-8")
    env = {**_base_env(tmp_path), "VOOL_BUNDLE_ROOT": str(app), "VOOL_HOME": str(home)}
    env.update({k: (str(chosen) if v == "chosen" else v) for k, v in override.items()})

    supervisor = BundleSupervisor(app, env=env)
    supervisor._prepare()

    # The supervisor reads the override as the API does (core.runtime_paths.vool_env) ...
    assert supervisor._notes_workspace_root() == chosen
    # ... and leaves the user's chosen folder alone, with no marker, so the copy still runs later.
    assert not (chosen / "notes").exists() and not (home / "workspace" / "notes" / "a.md").exists()
    assert not (home / "run" / bundle_supervisor._NOTES_COPY_MARKER).exists()
    assert (old / "a.md").is_file()


def test_the_copy_leaves_no_temporary_files_behind(tmp_path: Path) -> None:
    app, home = _install(tmp_path), _native_home(tmp_path)
    old = app / "app" / "notes"
    old.mkdir(parents=True)
    (old / "a.md").write_text("a\n", encoding="utf-8")

    BundleSupervisor(app, env={**_base_env(tmp_path), "VOOL_BUNDLE_ROOT": str(app), "VOOL_HOME": str(home)})._prepare()

    assert sorted(p.name for p in (home / "workspace" / "notes").iterdir()) == ["a.md"]


def test_without_hard_links_the_copy_still_lands_and_never_replaces(tmp_path: Path, monkeypatch) -> None:
    """FAT, exFAT and some network shares have no hard links: a VOOL_HOME there still migrates."""
    app, home = _install(tmp_path), _native_home(tmp_path)
    old = app / "app" / "notes"
    old.mkdir(parents=True)
    (old / "a.md").write_text("a\n", encoding="utf-8")
    (old / "b.md").write_text("old b\n", encoding="utf-8")
    new = home / "workspace" / "notes"
    new.mkdir(parents=True)
    (new / "b.md").write_text("new b\n", encoding="utf-8")

    def no_hard_links(src, dst, *args, **kwargs):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(bundle_supervisor.os, "link", no_hard_links)
    BundleSupervisor(app, env={**_base_env(tmp_path), "VOOL_BUNDLE_ROOT": str(app), "VOOL_HOME": str(home)})._prepare()

    assert (new / "a.md").read_text(encoding="utf-8") == "a\n"
    assert (new / "b.md").read_text(encoding="utf-8") == "new b\n"
    assert sorted(p.name for p in new.iterdir()) == ["a.md", "b.md"]
    assert (home / "run" / bundle_supervisor._NOTES_COPY_MARKER).is_file()
