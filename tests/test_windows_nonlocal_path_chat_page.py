"""On Windows, an image the model names on a share is refused by the route the chat page asks for it.

The chat page renders assistant markdown `![alt](target)` as an `<img>` whose `src` is
`/api/files/raw?path=<target>` when the target is a local-looking path (`/...` or `file://...`), and
keeps the raw path in `data-local` for the right-click actions. The browser fetches that `src` by
itself as soon as the answer is shown, so a model answer naming `//host/share/x.png` makes VOOL
look the path up with no click at all; on Windows that lookup is an SMB connection (and an NTLM hash
sent to whoever answers).

The page's own `renderRichText` runs under node (the harness of tests/test_chat_export_copy_ui.py);
each `src` it produces is then requested from the real served application with the Windows rule on,
and so is the page's "Reveal" action for the same image. `tests/_nonlocal_touch_recorder.py`
records (and refuses) every filesystem call or process start that names the marker host `attkr9`.
The fix is in the route, not the page: the page served with the Windows rule on is byte-for-byte the
page served with it off.
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from core import runtime_paths
from core.vool_chat_page import render_vool_chat_html
from tests._nonlocal_touch_recorder import recorder, windows_host
from tests.asgi_harness import asgi_request
from tests.test_chat_export_copy_ui import SHIM, _run_node, _slice, requires_node

HOST = {"Host": "127.0.0.1"}
JSON_HEADERS = {"Content-Type": "application/json", "Host": "127.0.0.1", "Origin": "http://127.0.0.1:11435"}
# Markdown image target -> the path the page asks the server for (and keeps in data-local).
IMAGES = {
    "//attkr9/s/x.png": "//attkr9/s/x.png",
    "/\\attkr9\\s\\x.png": "/\\attkr9\\s\\x.png",
    "file:////attkr9/s/x.png": "//attkr9/s/x.png",
}
_IMG = re.compile(r'<img class="chat-img" src="([^"]*)"[^>]*data-local="([^"]*)"')


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("VOOL_HOME", str(tmp_path / "home"))
    runtime_paths.configure_runtime_home(tmp_path / "home")
    yield
    runtime_paths.configure_runtime_home(None)


def _rendered_images() -> dict[str, tuple[str, str]]:
    """Each target's (src, data-local), as the page's own renderer writes them into an answer."""
    body = _slice("function esc(", "\n  el.innerHTML = html;\n}", include_end=True)
    answers = [f"Here is the chart you asked for:\n\n![chart]({target})\n" for target in IMAGES]
    out = _run_node(
        SHIM
        + body
        + "\nconst out = [];\n"
        + f"for (const answer of {json.dumps(answers)}) {{\n"
        + "  const el = new El();\n  renderRichText(el, answer);\n  out.push(el.innerHTML);\n}\n"
        + "process.stdout.write(JSON.stringify(out));\n"
    )
    images: dict[str, tuple[str, str]] = {}
    for target, rendered in zip(IMAGES, json.loads(out), strict=True):
        found = _IMG.findall(rendered)
        assert len(found) == 1, f"the page did not render {target!r} as one chat image: {rendered}"
        src, local = found[0]
        images[target] = (html.unescape(src), html.unescape(local))
    return images


@requires_node
def test_a_model_image_naming_a_share_is_refused_by_the_route_it_requests(recorder, windows_host) -> None:
    from apps.vool_api_server import create_app
    from core.web.api.runtime import RuntimeServices

    images = _rendered_images()
    app = create_app(RuntimeServices(display_name="VOOL"))
    # Off explicitly: on Windows the rule is already on when the test starts.
    windows_host.off()
    page_with_the_rule_off = render_vool_chat_html()
    windows_host.on()
    page_with_the_rule_on = render_vool_chat_html()

    fetched: dict[str, tuple[int, dict]] = {}
    revealed: dict[str, tuple[int, dict]] = {}
    for target, (src, local) in images.items():
        # The request the browser makes by itself for the <img>.
        status, _headers, raw = asgi_request(app, method="GET", path=src, headers=HOST)
        fetched[target] = (status, json.loads(raw or b"{}"))
        # The page's right-click "Reveal" for the same image (revealImage -> _fileOpen(local, true)).
        status, _headers, raw = asgi_request(
            app, method="POST", path="/api/files/open", headers=JSON_HEADERS,
            body=json.dumps({"path": local, "reveal": True}).encode("utf-8"),
        )
        revealed[target] = (status, json.loads(raw or b"{}"))

    assert recorder.touches == [] and recorder.marker_popen == [], f"{fetched}\n{revealed}\n{recorder.report()}"
    for target, (src, local) in images.items():
        # The page asked for exactly the path the answer named, through the Files route.
        assert urlsplit(src).path == "/api/files/raw", (target, src)
        assert parse_qs(urlsplit(src).query)["path"] == [IMAGES[target]] and local == IMAGES[target], (target, src, local)
    assert fetched == {target: (403, {"error": "not a known file"}) for target in IMAGES}
    assert revealed == {target: (403, {"error": "that file is not under a known Files folder"}) for target in IMAGES}
    # The page itself is not where the share is stopped: it is the same with the Windows rule on or off.
    assert page_with_the_rule_on == page_with_the_rule_off
