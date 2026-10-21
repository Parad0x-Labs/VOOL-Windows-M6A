"""Show in folder on Windows: File Explorer by argument list, never Finder, Preview or `open`.

Windows has no Finder, no Preview and no `open` command. There the Files panel and an answer image show
a file in File Explorer (``explorer.exe /select, <path>``), a project's folder button shows the folder in
File Explorer the same way, and opening a file is refused: Windows' default action for a .bat/.exe artifact is to
run it. These drive the real routes, the served chat page and its script with the host simulated as
Windows (``sys.platform``, the one check ``core.generated_files.host_is_windows`` makes) and every launcher
captured (subprocess.Popen, os.startfile, os.system), so nothing is ever started. The macOS argv stays pinned
in tests/test_generated_files.py and tests/test_chat_and_project_delete.py; the macOS page is compared here.

Directory links are junctions on Windows (no privilege needed) and symbolic links elsewhere; file links are
symbolic links on every platform. A host that cannot make a file symbolic link (Windows without Developer Mode
or an elevated shell) FAILS that case, naming the cause; it is never skipped.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from core import fs_links, runtime_paths
from core import generated_files as gf
from core.web.api.runtime import RuntimeServices
from core.web.api.service import dispatch_get, dispatch_post
from tests._windows_served_words import as_the_mac_page
from tests.chat_page_js_harness import DOM, run_node


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("VOOL_HOME", str(tmp_path / "home"))
    runtime_paths.configure_runtime_home(tmp_path / "home")
    yield
    runtime_paths.configure_runtime_home(None)


@pytest.fixture
def popen(monkeypatch, tmp_path):
    """Every launch of Explorer, `open` or anything naming this test's files, as (argv, kwargs), and every
    os.startfile and os.system call, as (["os.startfile" or "os.system", *args], kwargs).

    Nothing is started. A first dispatch also probes the GPU through Popen; those calls name none of
    these and are not recorded."""
    calls: list = []

    def _capture(args, **kwargs):
        argv = [str(a) for a in args]
        if os.path.basename(argv[0]).lower() in {"open", "explorer", "explorer.exe"} or any(
            str(tmp_path) in a for a in argv
        ):
            calls.append((list(args), kwargs))

    def _launcher(name):
        def _record(*args, **kwargs):
            calls.append(([name, *[str(a) for a in args]], kwargs))
            return 0
        return _record

    monkeypatch.setattr(subprocess, "Popen", _capture)
    monkeypatch.setattr(os, "startfile", _launcher("os.startfile"), raising=False)   # exists on Windows only
    monkeypatch.setattr(os, "system", _launcher("os.system"))
    return calls


@pytest.fixture
def root(tmp_path, monkeypatch):
    gen = tmp_path / "VOOL Renders"   # a space, as in a real Windows profile path
    gen.mkdir()
    monkeypatch.setattr(gf, "_default_roots", lambda: [gen])   # hermetic: only this Files root
    return gen


class _Host:
    """The simulated host. Its patches are undone when the test ends, before any other fixture's teardown."""

    def __init__(self, tmp_path: Path) -> None:
        self.patch = pytest.MonkeyPatch()
        self.windir = str(tmp_path / "Windows")
        self.exe = os.path.join(self.windir, "explorer.exe")

    def windows(self) -> str:
        """sys.platform says win32 and %SystemRoot% holds explorer.exe; returns explorer's path."""
        os.makedirs(self.windir, exist_ok=True)
        Path(self.exe).write_bytes(b"")   # never run: every launcher is captured
        self.patch.setenv("SystemRoot", self.windir)
        self.patch.setattr(sys, "platform", "win32")
        return self.exe

    def mac(self) -> None:
        self.patch.setattr(sys, "platform", "darwin")


@pytest.fixture
def host(tmp_path):
    """Requested last by each test, so it is torn down first: no other teardown runs as the simulated host."""
    simulated = _Host(tmp_path)
    yield simulated
    simulated.patch.undo()


def _dir_link(link: Path, target: Path) -> None:
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)
    assert fs_links.is_link(link)


def _file_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.fail(f"this host cannot create a file symbolic link ({exc}); on Windows that needs Developer Mode "
                    "or an elevated shell, so the case it sets up is unproven here, not passed")
    assert fs_links.is_link(link)


def _post(path, body):
    return dispatch_post(path=path, body=body, headers={"content-type": "application/json"},
                         runtime=RuntimeServices(display_name="VOOL"), model_name="vool",
                         workspace_root_provider=lambda: "/tmp", client_host="127.0.0.1")


def _j(response):
    return json.loads(response.body.decode("utf-8"))


def test_windows_files_click_selects_the_file_in_explorer_by_argument_list(root, popen, tmp_path, host) -> None:
    exe = host.windows()
    windows_folder = {"cwd": host.windir}   # Explorer starts in the Windows folder, never in VOOL's own
    render = root / "my render.png"
    render.write_bytes(b"png")
    real = os.path.realpath(render)

    res = _post("/api/files/open", {"path": str(render)})   # the Files panel's click: reveal by default
    assert res.status == 200 and _j(res) == {"ok": True}
    assert popen == [([exe, "/select,", real], windows_folder)]   # no shell: an argument list
    # What Explorer receives: the switch, then the path quoted on its own because it holds a space.
    assert subprocess.list2cmdline(popen[0][0]) == subprocess.list2cmdline([exe]) + f' /select, "{real}"'

    # The answer image's "Show in folder" asks with reveal: true -> the same Explorer call.
    res = _post("/api/files/open", {"path": str(render), "reveal": True})
    assert res.status == 200 and popen[-1] == ([exe, "/select,", real], windows_folder)
    assert len(popen) == 2


def test_windows_never_opens_or_starts_a_bat_or_exe_artifact(root, popen, tmp_path, host) -> None:
    exe = host.windows()
    for name in ("payload.bat", "setup.exe"):
        artifact = root / name
        artifact.write_bytes(b"@echo off")
        res = _post("/api/files/open", {"path": str(artifact), "reveal": False})   # the old "Open in Preview"
        assert res.status == 501
        assert _j(res) == {"ok": False, "error": "opening a file is not available on Windows yet"}
        assert gf.open_file(str(artifact), reveal=False) is False   # the module refuses on its own too
    assert popen == []
    # Nothing in the module can open a file the Windows way, by any launcher.
    source = inspect.getsource(gf)
    for launcher in ("startfile", "ShellExecute", "shell32", "windll", "shell=True"):
        assert launcher not in source, launcher

    # Show in folder on the same artifact: Explorer selects it; the artifact itself is never argv[0].
    bat = os.path.realpath(root / "payload.bat")
    assert _post("/api/files/open", {"path": str(root / "payload.bat")}).status == 200
    assert popen == [([exe, "/select,", bat], {"cwd": host.windir})]

    # macOS is unchanged: the same request still opens the file with `open`.
    host.mac()
    assert _post("/api/files/open", {"path": str(root / "payload.bat"), "reveal": False}).status == 200
    assert popen[-1] == (["open", bat], {})


def test_windows_show_in_folder_refuses_anything_outside_the_files_roots(root, popen, tmp_path, host) -> None:
    exe = host.windows()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.png"
    secret.write_bytes(b"x")
    _file_link(root / "innocent.png", secret)   # inside by name, outside by realpath
    _dir_link(root / "elsewhere", outside)       # a folder link out of the root
    for path in (str(secret), str(root / "innocent.png"), str(root / "elsewhere" / "secret.png"),
                 str(root / ".." / "outside" / "secret.png")):
        res = _post("/api/files/open", {"path": path})
        assert res.status == 403, path
        assert gf.open_file(path) is False

    # Explorer splits its command line on commas, and that it keeps a quoted path with a comma whole is not
    # proven: a path with a comma is refused, with or without a space that makes subprocess quote it.
    assert gf._explorer_argv("/select,", r"C:\Renders\a,b.png") is None   # unquoted: certainly two paths
    for name in ("a,b.png", "a, b.png"):
        comma = root / name
        comma.write_bytes(b"x")
        res = _post("/api/files/open", {"path": str(comma)})
        assert res.status == 500 and _j(res) == {"ok": False}, name
    # A file that is gone is not handed to Explorer to guess at.
    assert _post("/api/files/open", {"path": str(root / "gone.png")}).status == 500
    # No Windows folder to find explorer.exe in: nothing starts, never a bare "explorer.exe".
    shown = root / "shown.png"
    shown.write_bytes(b"x")
    host.patch.delenv("SystemRoot")
    host.patch.delenv("windir", raising=False)
    assert _post("/api/files/open", {"path": str(shown)}).status == 500
    assert popen == []

    host.patch.setenv("SystemRoot", os.path.dirname(exe))
    assert _post("/api/files/open", {"path": str(shown)}).status == 200
    assert popen == [([exe, "/select,", os.path.realpath(shown)], {"cwd": host.windir})]


def test_windows_show_in_folder_hands_explorer_only_the_path_it_checked(root, popen, tmp_path, host) -> None:
    """A file swapped for a link out of the root right after open_file's root check is refused, not selected."""
    exe = host.windows()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.bat").write_bytes(b"@echo off")
    shown = root / "shown.png"
    shown.write_bytes(b"x")
    checked = gf.path_is_allowed
    swaps: list[str] = []

    def _then_swapped(path):
        allowed = checked(path)
        if not swaps:   # another program swaps the file for a link once, right after the first check
            swaps.append(path)
            shown.unlink()
            _file_link(shown, outside / "secret.bat")
        return allowed

    host.patch.setattr(gf, "path_is_allowed", _then_swapped)
    assert gf.open_file(str(shown)) is False
    assert swaps == [str(shown)] and popen == []

    # Unswapped, the same request selects the file.
    host.patch.setattr(gf, "path_is_allowed", checked)
    shown.unlink()
    shown.write_bytes(b"x")
    assert _post("/api/files/open", {"path": str(shown)}).status == 200
    assert popen == [([exe, "/select,", os.path.realpath(shown)], {"cwd": host.windir})]


def test_windows_project_button_shows_the_folder_in_explorer(popen, tmp_path, host) -> None:
    folder = tmp_path / "My Project"
    folder.mkdir()
    pids = {}
    for name in ("a,b", "a, b"):
        (tmp_path / name).mkdir()
        pids[name] = _j(_post("/api/projects", {"name": name, "root": str(tmp_path / name)}))["project"]["id"]
    pid = _j(_post("/api/projects", {"name": "P", "root": str(folder)}))["project"]["id"]
    exe = host.windows()
    popen.clear()   # registering may probe hardware through Popen; only the reveal matters here

    res = _post("/api/projects/reveal", {"id": pid})
    assert res.status == 200 and _j(res) == {"ok": True, "id": pid}
    real = os.path.realpath(folder)
    assert popen == [([exe, "/select,", real], {"cwd": host.windir})]   # selected, never opened
    assert subprocess.list2cmdline(popen[0][0]) == subprocess.list2cmdline([exe]) + f' /select, "{real}"'

    # A folder name Explorer could split on its commas is refused, not guessed at.
    popen.clear()
    for name, comma_id in pids.items():
        res = _post("/api/projects/reveal", {"id": comma_id})
        assert res.status == 200 and _j(res)["ok"] is False, name
    assert popen == []


def test_windows_project_button_refuses_a_folder_swapped_for_a_link(popen, tmp_path, host) -> None:
    folder = tmp_path / "proj"
    folder.mkdir()
    pid = _j(_post("/api/projects", {"name": "P", "root": str(folder)}))["project"]["id"]
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    folder.rmdir()
    _dir_link(folder, outside)   # the registered folder now leads elsewhere
    host.windows()
    popen.clear()

    res = _post("/api/projects/reveal", {"id": pid})
    assert res.status == 200 and _j(res) == {"ok": False, "id": pid}
    assert popen == []   # neither the link nor its target is opened

    # Swapped for a file (Explorer would run it): not a registered folder any more, as before.
    if os.name == "nt":
        os.rmdir(folder)   # a junction is removed as a folder
    else:
        folder.unlink()
    folder.write_bytes(b"@echo off")
    assert _post("/api/projects/reveal", {"id": pid}).status == 404
    assert _post("/api/projects/reveal", {"id": "proj_000000000000"}).status == 404
    assert popen == []


def _served_chat(monkeypatch, platform: str) -> str:
    with monkeypatch.context() as patch:   # the host is simulated for the render only
        patch.setattr(sys, "platform", platform)
        res = dispatch_get(path="/chat", query={}, runtime=RuntimeServices(display_name="VOOL"), model_name="vool")
    assert res.status == 200
    return res.body.decode("utf-8")


# (macOS text, Windows text) as the served page carries them.
_TEXTS = (
    ("click a file to reveal it in Finder.", "click a file to show it in its folder."),
    ("prev.title = 'Reveal folder in Finder';", "prev.title = 'Show folder in File Explorer';"),
    ('title="Click to open in Preview - right-click for more"',
     'title="Opening files is not available on Windows yet - right-click for more"'),
    ("add('Open in Preview',", "'Open (not available on Windows yet)'"),
    ("add('Show in Finder',", "add('Show in folder',"),
)


def test_served_chat_page_says_show_in_folder_only_on_a_windows_host(monkeypatch) -> None:
    mac = _served_chat(monkeypatch, "darwin")
    linux = _served_chat(monkeypatch, "linux")
    windows = _served_chat(monkeypatch, "win32")
    assert mac == linux   # every host but Windows is served the same page
    for mac_text, windows_text in _TEXTS:
        assert mac.count(mac_text) == 1 and mac_text not in windows, mac_text
        assert windows.count(windows_text) == 1 and windows_text not in mac, windows_text
    assert "_fileOpen(local, false)" in mac and "_fileOpen(local, false)" not in windows
    assert as_the_mac_page(windows) == mac   # nothing else on the page differs


# Drives the page's own handlers: a left click on an answer image, then every item of its right-click
# menu (even a disabled one), recording each POST to /api/files/open.
_IMAGE_ACTIONS = r"""
const opened = [];
globalThis.fetch = async (url, opts) => {
  if (String(url) === '/api/files/open') opened.push(JSON.parse(opts.body));
  return { ok: true, status: 200, headers: { get: () => null }, json: async () => ({ ok: true }),
           text: async () => '{}', blob: async () => ({}) };
};
const tag = renderInline('![render](file:///gen/payload.bat)');
const img = { dataset: { local: '/gen/payload.bat' }, src: '' };
const on = (sel) => (sel === 'img.chat-img' ? img : null);
logEl.__on.click({ target: { closest: on }, preventDefault() {} });
const afterClick = opened.slice();
logEl.__on.contextmenu({ target: { closest: on }, preventDefault() {}, clientX: 5, clientY: 5 });
const menu = document.body.children.filter((c) => c.id === 'imgMenu').pop();
const items = menu.children.map((b) => ({ label: b.textContent, disabled: !!b.disabled }));
for (const b of menu.children) b.__click();
out({ title: (tag.match(/title="([^"]*)"/) || [])[1] || '', afterClick, items, opened });
"""


def _drive_image_actions(page: str) -> dict:
    scripts = sorted(re.findall(r"<script[^>]*>(.*?)</script>", page, re.DOTALL), key=len, reverse=True)
    result = run_node(DOM + "\n" + scripts[0] + "\n" + _IMAGE_ACTIONS)
    assert result["errors"] == []
    return result


def test_windows_image_click_and_menu_never_ask_to_open_a_file(monkeypatch) -> None:
    windows = _drive_image_actions(_served_chat(monkeypatch, "win32"))
    assert windows["title"] == "Opening files is not available on Windows yet - right-click for more"
    assert windows["afterClick"] == []   # a click starts nothing
    assert windows["items"] == [
        {"label": "Open (not available on Windows yet)", "disabled": True},
        {"label": "Show in folder", "disabled": False},
        {"label": "Copy image", "disabled": False},
        {"label": "Copy path", "disabled": False},
    ]
    assert windows["opened"] == [{"path": "/gen/payload.bat", "reveal": True}]   # only Show in folder

    mac = _drive_image_actions(_served_chat(monkeypatch, "darwin"))   # macOS unchanged
    assert mac["title"] == "Click to open in Preview - right-click for more"
    assert mac["afterClick"] == [{"path": "/gen/payload.bat", "reveal": False}]
    assert [item["label"] for item in mac["items"]] == ["Open in Preview", "Show in Finder", "Copy image", "Copy path"]
    assert mac["opened"] == [{"path": "/gen/payload.bat", "reveal": False}, {"path": "/gen/payload.bat", "reveal": False},
                             {"path": "/gen/payload.bat", "reveal": True}]
