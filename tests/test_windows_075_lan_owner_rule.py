"""0.7.5 lane 3, the LAN owner rule on Windows: the Windows-only callers still reach the owner path.

Mac 0.7.5 makes an unknown peer a remote turn (`client_host` defaults to "", loopback is parsed as an
IP). The Windows-only callers were audited for that (jobs/catchup-075/lan-owner-rule/FINDINGS.md):
- The `vool://` protocol handler posts the sign-in callback to 127.0.0.1, which the runtime takes only
  from a loopback peer. urllib sent it to the proxy in HTTP_PROXY or the Windows Internet settings, so
  the runtime saw the proxy as the peer and refused it. It now posts with no proxy.
- The Discord bridge's owner file (Mac 0.7.5) follows the runtime's own home on Windows, is read and
  replaced past a sharing violation, and is written owner-only.
All of it is Windows-only (Jesse, 16 Nov); the off-Windows rows show the Mac behaviour is kept.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

import relay.bridge_workers.discord_bridge as dbr
from core import windows_sharing_retry
from installer.bundle import vool_protocol_handler as handler


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(dbr, "_IS_WINDOWS", True)
    monkeypatch.setattr(windows_sharing_retry, "_IS_WINDOWS", True)
    monkeypatch.setattr(windows_sharing_retry, "_DELAYS", (0.0,) * 10)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """No VOOL_HOME, only the legacy NULLA_HOME spelling the runtime also honours."""
    monkeypatch.delenv("VOOL_HOME", raising=False)
    monkeypatch.delenv("VOOL_DISCORD_OWNER_USER_ID", raising=False)
    monkeypatch.setenv("NULLA_HOME", str(tmp_path / "nulla-home"))
    return tmp_path / "nulla-home"


def test_on_windows_the_owner_file_follows_the_runtimes_own_home(windows, home) -> None:
    from core.runtime_paths import active_vool_home

    assert dbr._owner_file() == active_vool_home() / "config" / "discord_bridge.json"
    assert dbr._owner_file().is_relative_to(home.resolve())


def test_off_windows_the_owner_file_is_where_the_mac_release_puts_it(home, monkeypatch) -> None:
    monkeypatch.setattr(dbr, "_IS_WINDOWS", False)
    assert dbr._owner_file() == Path.home() / ".vool_runtime" / "config" / "discord_bridge.json"


def _flaky(real, failures: int):
    state = {"left": failures}

    def call(*args, **kwargs):
        if state["left"]:
            state["left"] -= 1
            raise PermissionError(32, "The process cannot access the file because it is being used")
        return real(*args, **kwargs)

    return call


def test_on_windows_a_save_survives_a_sharing_violation_and_is_written_owner_only(windows, home, monkeypatch) -> None:
    import os

    written: list[Path] = []
    from core import owner_only

    real_write = owner_only.write_private_file
    monkeypatch.setattr(owner_only, "write_private_file",
                        lambda path, data, **k: written.append(Path(path)) or real_write(path, data, **k))
    monkeypatch.setattr(os, "replace", _flaky(os.replace, 2))
    bridge = dbr.DiscordBridge()
    bridge._save_owner("111")
    assert written == [dbr._owner_file()]
    assert json.loads(dbr._owner_file().read_text(encoding="utf-8")) == {"owner_user_id": "111"}
    assert dbr.DiscordBridge().owner_user_id == "111"


def test_on_windows_a_read_refused_for_a_moment_still_finds_the_owner(windows, home, monkeypatch) -> None:
    dbr._owner_file().parent.mkdir(parents=True)
    dbr._owner_file().write_text(json.dumps({"owner_user_id": "111"}), encoding="utf-8")
    monkeypatch.setattr(windows_sharing_retry, "open", _flaky(open, 2), raising=False)
    assert dbr.DiscordBridge().owner_user_id == "111"


def test_sabotage_without_the_retry_one_refused_read_locks_out_every_user(windows, home, monkeypatch) -> None:
    """The control: the plain read the Mac code does turns one sharing violation into "unreadable"."""
    dbr._owner_file().parent.mkdir(parents=True)
    dbr._owner_file().write_text(json.dumps({"owner_user_id": "111"}), encoding="utf-8")
    monkeypatch.setattr(windows_sharing_retry, "read_text", lambda path, **k: (_ for _ in ()).throw(
        PermissionError(32, "being used")))
    assert dbr.DiscordBridge().owner_user_id is dbr._OWNER_UNREADABLE


class _Callback(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        type(self).seen.append(json.loads(self.rfile.read(length)))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *_args):
        pass


@pytest.fixture
def runtime_callback(monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), _Callback)
    _Callback.seen = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(handler, "_API_CALLBACK_URL",
                        f"http://127.0.0.1:{server.server_address[1]}/api/auth/openrouter/callback")
    # A proxy that is not there, as a machine with a proxy in its settings but no route to it has.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    yield _Callback.seen
    server.shutdown()
    server.server_close()


def test_the_sign_in_callback_reaches_the_runtime_directly_whatever_proxy_is_set(runtime_callback) -> None:
    assert handler._post_callback("the-code", "the-state") is True
    assert runtime_callback == [{"code": "the-code", "state": "the-state"}]


def test_sabotage_through_the_proxy_the_callback_never_reaches_the_runtime(runtime_callback, monkeypatch) -> None:
    """The control: urllib's default opener takes the proxy from the environment."""
    import urllib.request

    monkeypatch.setattr(handler, "_LOOPBACK_OPENER", urllib.request.build_opener())
    assert handler._post_callback("the-code", "the-state") is False
    assert runtime_callback == []


def _place(kind: str, monkeypatch) -> None:
    """An owner file VOOL cannot read: broken JSON, a link loop, access denied on its status, or a file
    where config/ should be. The loop and the denial are the errors Windows gives, raised for that one
    path, so the row needs no symlink privilege."""
    import errno
    import os

    path = dbr._owner_file()
    if kind in ("link loop", "access denied"):
        code = errno.ELOOP if kind == "link loop" else errno.EACCES
        real_stat = os.stat

        def stat(target, *args, **kwargs):
            if Path(target) == path:
                raise OSError(code, os.strerror(code), str(target))
            return real_stat(target, *args, **kwargs)

        monkeypatch.setattr(dbr.os, "stat", stat)
        return
    if kind == "folder in the way":
        path.parent.parent.mkdir(parents=True)
        path.parent.write_text("not a folder\n", encoding="utf-8")
        return
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")


@pytest.mark.parametrize("kind", ["broken json", "link loop", "access denied", "folder in the way"])
def test_on_windows_an_owner_file_that_cannot_be_read_refuses_everyone(windows, home, kind, monkeypatch) -> None:
    """Chief of Staff, 25 Nov: only a file that is not there pairs the first speaker."""
    _place(kind, monkeypatch)
    bridge = dbr.DiscordBridge()
    assert bridge.owner_user_id is dbr._OWNER_UNREADABLE
    assert bridge._authorize("222") is False
    assert bridge._authorize("111") is False


def test_on_windows_no_owner_file_at_all_still_pairs_the_first_user(windows, home) -> None:
    bridge = dbr.DiscordBridge()
    assert bridge.owner_user_id is None
    assert bridge._authorize("111") is True
    assert bridge._authorize("222") is False
