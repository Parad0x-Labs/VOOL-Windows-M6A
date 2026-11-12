"""Windows ``vool://`` protocol handler for the bundled VOOL installation.

An OpenRouter sign-in callback (``vool://auth/openrouter/callback``) is validated
strictly, the bundle supervisor is started or reused when necessary, and the two
OAuth parameters are forwarded to the shared desktop callback endpoint. It never
exchanges the code, checks PKCE state itself, or writes the callback URI to a log
or file.

Any other ``vool:`` link (``vool://`` from Win+R, a bookmark) opens VOOL the way
the Start-menu shortcut does -- which brings an open window forward -- and uses
nothing from the link. A link that is refused, or a callback that cannot be
delivered, is reported in a message box instead of failing silently; the message
never repeats the link.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlsplit
from urllib.request import Request, urlopen

_API_CALLBACK_URL = "http://127.0.0.1:11435/api/auth/openrouter/callback"
_RETRY_COUNT = 30
_RETRY_DELAY_SECONDS = 1.0
_MAX_CODE_LENGTH = 4096
_MAX_STATE_LENGTH = 1024
_MAX_URI_LENGTH = 16384
# vool:auth/... and vool:/auth/... are sign-in links too, held to the same strict check.
_AUTH_LINK = re.compile(r"/*auth(?:[/?#]|$)", re.IGNORECASE)
_TITLE = "VOOL"
_REFUSED_LINK = "VOOL did not open this link: it is not a VOOL link it can use."
_REFUSED_SIGN_IN = (
    "VOOL did not accept this sign-in link: it is not a valid OpenRouter sign-in callback. "
    "Start the sign-in again from VOOL."
)
_SIGN_IN_UNDELIVERED = (
    "VOOL could not finish the OpenRouter sign-in: VOOL did not answer in time. "
    "Open VOOL and start the sign-in again."
)
_NOT_STARTED = "VOOL could not be started from this link. Open VOOL from the Start menu."


def parse_callback_uri(uri: str) -> tuple[str, str]:
    """Return decoded ``(code, state)`` from the exact VOOL callback shape."""
    if not isinstance(uri, str) or len(uri) > _MAX_URI_LENGTH:
        raise ValueError("callback URI is invalid")
    parsed = urlsplit(uri)
    if parsed.scheme.lower() != "vool" or parsed.netloc.lower() != "auth":
        raise ValueError("callback URI must use vool://auth")
    if parsed.path.rstrip("/").lower() != "/openrouter/callback" or parsed.fragment:
        raise ValueError("callback URI path is invalid")
    pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=False)
    values: dict[str, str] = {}
    for key, value in pairs:
        key = key.strip().lower()
        if key not in {"code", "state"} or key in values:
            raise ValueError("callback URI query is invalid")
        values[key] = value
    code = values.get("code", "").strip()
    state = values.get("state", "").strip()
    if (
        not code
        or not state
        or len(code) > _MAX_CODE_LENGTH
        or len(state) > _MAX_STATE_LENGTH
        or any(ord(char) < 0x20 for char in code + state)
    ):
        raise ValueError("callback URI must contain valid code and state")
    return code, state


def _post_callback(code: str, state: str) -> bool:
    body = json.dumps({"code": code, "state": state}, separators=(",", ":")).encode("utf-8")
    request = Request(
        _API_CALLBACK_URL,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=2) as response:
            return 200 <= int(response.status) < 300
    except (HTTPError, OSError, URLError, ValueError):
        return False


# AllowSetForegroundWindow(ASFW_ANY): the DWORD -1.
_ASFW_ANY = 0xFFFFFFFF


def _pass_on_the_foreground() -> bool:
    """Let VOOL's own window come to the front when this link starts or reopens it.

    Windows lets only a process the user just started (this handler, launched by the browser or
    the shell) take the foreground. VOOL's supervisor and window are three launches further down
    (cmd, VOOL.cmd, pythonw), so their SetForegroundWindow was refused and the link seemed to do
    nothing. ASFW_ANY hands this one-time right on; Windows ends it at the next user input.
    Windows only; never fails the link. True when Windows accepted it."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        allow = ctypes.windll.user32.AllowSetForegroundWindow
        allow.argtypes = (wintypes.DWORD,)
        allow.restype = wintypes.BOOL
        return bool(allow(_ASFW_ANY))
    except Exception:
        return False


def _wscript() -> str:
    """``%SystemRoot%\\System32\\wscript.exe`` by absolute path, or OSError.

    A bare name would be looked up through pythonw's own folder, the current folder and PATH first.
    Only a drive-letter SystemRoot is trusted (never a network share); anything else fails closed.
    The same rule as core/process_liveness.py ``_system32``, repeated here because this handler
    runs from the top of the install and imports nothing of VOOL's."""
    import ntpath

    root = os.environ.get("SystemRoot") or ""
    drive, rest = ntpath.splitdrive(root)
    if len(drive) != 2 or drive[1] != ":" or not rest.startswith(("\\", "/")):
        raise OSError("SystemRoot is not an absolute Windows folder")
    exe = ntpath.join(root, "System32", "wscript.exe")
    if not os.path.isfile(exe):
        raise OSError(f"{exe} is missing")
    return exe


def _start_bundle(root: Path) -> None:
    """Start VOOL exactly the way the Start-menu shortcut does: ``wscript.exe vool.vbs``.

    Steve round 18 (578d185f): starting VOOL.cmd from here through ``cmd /s /c`` with
    CREATE_NO_WINDOW|DETACHED_PROCESS and null handles never reached the supervisor (the handler
    exited 0 and nothing happened), while the same VOOL.cmd run by vool.vbs works. vool.vbs runs
    VOOL.cmd hidden from wscript, a GUI process, which gives VOOL.cmd's ``for /f`` and nested
    ``start "" /B`` the hidden console they expect. VOOL.cmd is started directly only when the
    install has no vool.vbs."""
    script = root / "vool.vbs"
    if script.is_file():
        _pass_on_the_foreground()
        subprocess.Popen(
            [_wscript(), str(script)],   # a list: each path is quoted for CreateProcess, & ( ) % included
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        return
    launcher = root / "VOOL.cmd"
    if not launcher.is_file():
        raise FileNotFoundError("VOOL.cmd is missing")
    if any(char in str(launcher) for char in '"%\r\n'):
        # Inside quotes cmd still expands %NAME% and ends the line at a line break.
        raise ValueError("the VOOL.cmd path holds a quote, % or line break cmd cannot keep")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    _pass_on_the_foreground()
    subprocess.Popen(
        # A string, not a list: /s with doubled quotes keeps the path quoted even when it holds & or ( ).
        f'"{os.environ.get("ComSpec", "cmd.exe")}" /s /c ""{launcher}""',
        cwd=str(root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
        close_fds=True,
    )


def deliver_callback(uri: str, *, root: Path | None = None, sleep=time.sleep) -> bool:
    """Validate and deliver a callback through the shared desktop runtime."""
    code, state = parse_callback_uri(uri)
    bundle_root = (root or _bundle_root()).resolve()
    started = False
    for attempt in range(_RETRY_COUNT):
        if _post_callback(code, state):
            return True
        if not started:
            _start_bundle(bundle_root)
            started = True
        if attempt + 1 < _RETRY_COUNT:
            sleep(_RETRY_DELAY_SECONDS)
    return False


def _bundle_root() -> Path:
    return Path(__file__).resolve().parent


def _notify(message: str) -> None:
    """Tell the user, who launched this from a link with no console (pythonw), what happened."""
    if os.name == "nt":
        try:
            import ctypes

            mb_ok, mb_iconwarning, mb_setforeground = 0x0, 0x30, 0x10000
            ctypes.windll.user32.MessageBoxW(None, message, _TITLE, mb_ok | mb_iconwarning | mb_setforeground)
            return
        except Exception:
            pass
    with contextlib.suppress(Exception):
        print(message, file=sys.stderr)


def classify_uri(uri: object) -> str:
    """``"callback"`` for a link addressed to vool://auth, ``"open"`` for any other vool: link,
    ``"refused"`` for anything else. Only the scheme and host are read; nothing in an "open"
    link is used for anything."""
    if not isinstance(uri, str) or not uri or len(uri) > _MAX_URI_LENGTH:
        return "refused"
    scheme, sep, rest = uri.partition(":")
    if not sep or scheme.lower() != "vool":
        return "refused"
    try:
        host = urlsplit(uri).netloc.lower()
    except ValueError:
        host = ""
    if host == "auth" or _AUTH_LINK.match(rest):
        return "callback"
    return "open"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        return 2
    kind = classify_uri(args[0])
    if kind == "refused":
        _notify(_REFUSED_LINK)
        return 1
    if kind == "open":
        try:
            _start_bundle(_bundle_root())
        except (FileNotFoundError, OSError, ValueError):
            _notify(_NOT_STARTED)
            return 1
        return 0
    try:
        delivered = deliver_callback(args[0])
    except ValueError:
        _notify(_REFUSED_SIGN_IN)
        return 1
    except (FileNotFoundError, OSError):
        _notify(_NOT_STARTED)
        return 1
    if not delivered:
        _notify(_SIGN_IN_UNDELIVERED)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
