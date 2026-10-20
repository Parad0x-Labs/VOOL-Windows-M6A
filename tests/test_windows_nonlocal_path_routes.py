"""On Windows, a served route that is handed a share or a partial path refuses it before anything reaches for it.

A stat of `\\\\host\\share` on Windows is an SMB connection (and an NTLM hash sent to whoever
answers), so a route that answers 403 or 404 only after resolving the path has already done the
damage. Every request here goes through the real served application
(`apps.vool_api_server.create_app` driven by `tests/asgi_harness.asgi_request`), and
`tests/_nonlocal_touch_recorder.py` records (and refuses) every filesystem call or process start
that names the marker host `attkr9`.

What this file pins:

- `/api/files/raw` and `/api/files/open` answer their existing 403s for every form, with no lookup;
  a local relative name inside a Files folder is still served.
- The session bundle download answers its existing 403 `path_not_allowed`; inspect and import answer
  `BUNDLE_PATH_NOT_LOCAL` with the sentence. A local relative name still reaches each route's own
  existence check.
- A new project on a share is refused with the sentence; a share already stored in `projects.json`
  is listed as missing and cannot be revealed, without being probed. A stored local project is still
  probed and found.
- The Media Studio editor refuses to open or export to a share, before the media worker is asked and
  before the Windows write gate places the output; local relative paths still open and export.
- A chat request whose body or `source_context` names a share as its workspace is refused before
  any turn runs, on `/api/chat` and `/v1/chat/completions`; a chat with no such field still runs.
- A `/api/mode` workspace claim naming a share is the existing mismatch, decided without resolving
  the claim; a local spelling of the chat's real folder still matches.

The Windows rule is switched on only through `windows_path_policy._IS_WINDOWS` (the `windows_host`
fixture), and only after local setup, because with it on a POSIX `tmp_path` counts as
root-relative; the controls therefore use relative paths. Literal sentences are asserted, never
`refusal_text`, so this file runs unchanged against a base that has no such module.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from core import media_studio_service as media
from core import mode_permission_policy as permission
from core import project_store, runtime_paths
from core import windows_write_gate as gate
from tests._nonlocal_touch_recorder import MARKER_FORMS, NETWORK_FORMS, MarkerForm, recorder, windows_host
from tests.asgi_harness import asgi_request

STATUS = "non_local_path_refused"
HOST = {"Host": "127.0.0.1"}
# What the page's own fetches send: JSON from the page's loopback origin.
JSON_HEADERS = {"Content-Type": "application/json", "Host": "127.0.0.1", "Origin": "http://127.0.0.1:11435"}
CHAT_FIELDS = ("workspace", "workspace_root", "cwd", "projectRoot")
PNG = b"\x89PNG\r\n\x1a\nnot-really-an-image"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("VOOL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("VOOL_WORKSPACE_ROOT", str(tmp_path / "default-workspace"))
    runtime_paths.configure_runtime_home(tmp_path / "home")
    yield
    runtime_paths.configure_runtime_home(None)


def _sentence(form: MarkerForm) -> str:
    return "is a network or device path" if form.network else "is not a full local path"


def _file_on(form: MarkerForm, name: str) -> str:
    """`name` inside the form's folder, in the form's own separator, so the kind does not change."""
    return form.value + ("\\" if "\\" in form.value else "/") + name


def _app(agent: Any = None):
    from apps.vool_api_server import create_app
    from core.web.api.runtime import RuntimeServices

    return create_app(RuntimeServices(display_name="VOOL", agent=agent))


def _json(raw: bytes) -> dict[str, Any]:
    return json.loads(raw.decode("utf-8") or "{}")


def _get(app, path: str) -> tuple[int, dict[str, Any]]:
    status, _headers, raw = asgi_request(app, method="GET", path=path, headers=HOST)
    return status, _json(raw)


def _post(app, path: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    status, _headers, raw = asgi_request(
        app, method="POST", path=path, headers=JSON_HEADERS, body=json.dumps(payload).encode("utf-8")
    )
    return status, _json(raw)


def _session() -> str:
    return f"openclaw:{uuid.uuid4().hex[:20]}"


def test_files_raw_and_open_refuse_a_share_without_touching_it(
    tmp_path: Path, monkeypatch, recorder, windows_host
) -> None:
    # A real Files folder holding a real render, so the routes have something they may serve.
    renders = tmp_path / "renders"
    renders.mkdir()
    (renders / "render.png").write_bytes(PNG)
    monkeypatch.setenv("VOOL_FILES_DIRS", str(renders))
    app = _app()
    windows_host.on()

    observed: dict[str, tuple[tuple[int, dict], tuple[int, dict]]] = {}
    for form in MARKER_FORMS:
        # The page asks for an inline image as /api/files/raw?path=<encodeURIComponent(path)>, and its
        # right-click "Reveal" posts {path, reveal: true} to /api/files/open.
        raw = _get(app, "/api/files/raw?path=" + quote(form.value, safe=""))
        opened = _post(app, "/api/files/open", {"path": form.value, "reveal": True})
        observed[form.id] = (raw, opened)
    # Control: a local relative name for the same render, from inside the Files folder, is still served.
    monkeypatch.chdir(renders)
    local_status, _headers, local_bytes = asgi_request(
        app, method="GET", path="/api/files/raw?path=render.png", headers=HOST
    )

    assert recorder.touches == [] and recorder.marker_popen == [], f"{observed}\n{recorder.report()}"
    opener_starts = [spawn for spawn in recorder.popen if any("core/generated_files.py" in f for f in spawn.frames)]
    assert opener_starts == [], "a file opener was started for a refused path"
    assert observed == {
        form.id: (
            (403, {"error": "not a known file"}),
            (403, {"error": "that file is not under a known Files folder"}),
        )
        for form in MARKER_FORMS
    }
    assert (local_status, local_bytes) == (200, PNG)


def test_session_bundle_download_inspect_and_import_refuse_a_share(
    tmp_path: Path, monkeypatch, recorder, windows_host
) -> None:
    app = _app()
    bundles = runtime_paths.data_path("session_bundles")
    bundles.mkdir(parents=True, exist_ok=True)
    windows_host.on()

    downloads: dict[str, tuple[int, dict]] = {}
    imports: dict[str, list[tuple[int, dict]]] = {}
    for form in MARKER_FORMS:
        bundle = _file_on(form, "b.voolsession")
        downloads[form.id] = _get(app, "/api/session/bundle/download?path=" + quote(bundle, safe=""))
        imports[form.id] = [
            _post(app, "/api/session/bundle/inspect-import", {"path": bundle}),
            _post(app, "/api/session/bundle/import", {"path": bundle}),
        ]
    # Control: a local relative name in the bundle folder still reaches each route's own check, which
    # finds no such file.
    monkeypatch.chdir(bundles)
    local_download = _get(app, "/api/session/bundle/download?path=missing.voolsession")
    local_inspect = _post(app, "/api/session/bundle/inspect-import", {"path": "missing.voolsession"})

    assert recorder.touches == [] and recorder.marker_popen == [], f"{downloads}\n{imports}\n{recorder.report()}"
    assert downloads == {form.id: (403, {"ok": False, "error": "path_not_allowed"}) for form in MARKER_FORMS}
    for form in MARKER_FORMS:
        for status, body in imports[form.id]:
            assert (status, body.get("ok"), body.get("code")) == (200, False, "BUNDLE_PATH_NOT_LOCAL"), (form.id, body)
            assert _sentence(form) in str(body.get("error") or ""), (form.id, body)
    assert local_download == (404, {"ok": False, "error": "not_found"})
    assert (local_inspect[0], local_inspect[1].get("code")) == (200, "BUNDLE_PATH_MISSING"), local_inspect


def test_project_create_refuses_a_share_and_a_stored_share_is_never_probed(
    tmp_path: Path, monkeypatch, recorder, windows_host
) -> None:
    app = _app()
    folder = tmp_path / "acme"
    folder.mkdir()
    status, created = _post(app, "/api/projects", {"name": "Acme", "root": str(folder)})
    assert status == 200, created
    local_id = created["project"]["id"]
    # Shares already in projects.json, as an older build or a hand edit would have stored them. A
    # stored root is realpath output, so only the network spellings can be there.
    stored = {f"proj_share{index}": form for index, form in enumerate(NETWORK_FORMS)}
    registry = json.loads(project_store.projects_path().read_text(encoding="utf-8"))
    registry["projects"] += [
        {"id": pid, "name": f"share {form.id}", "root": form.value, "created_at": "2026-01-01T00:00:00Z"}
        for pid, form in stored.items()
    ]
    project_store.projects_path().write_text(json.dumps(registry), encoding="utf-8")
    windows_host.on()

    refused = {form.id: _post(app, "/api/projects", {"name": "Share", "root": form.value}) for form in MARKER_FORMS}
    listing_status, listing = _get(app, "/api/projects")
    revealed = {pid: _post(app, "/api/projects/reveal", {"id": pid}) for pid in stored}

    assert recorder.touches == [] and recorder.marker_popen == [], f"{refused}\n{listing}\n{recorder.report()}"
    for form in MARKER_FORMS:
        status, body = refused[form.id]
        assert status == 400 and _sentence(form) in str(body.get("error") or ""), (form.id, status, body)
    assert listing_status == 200, listing
    exists = {entry["id"]: entry["exists"] for entry in listing["projects"]}
    # Nothing was registered for a share, every stored share lists as missing, and the stored local
    # project is still probed and found.
    assert exists == {local_id: True, **{pid: False for pid in stored}}
    assert revealed == {pid: (404, {"error": "project folder not found"}) for pid in stored}


def test_media_editor_open_and_export_refuse_a_share(tmp_path: Path, monkeypatch, recorder, windows_host) -> None:
    # The media worker is a process of its own that needs ffmpeg; a recorder of what it is asked
    # stands in for it, as in tests/test_media_editor_export_asks_the_windows_gate.py.
    asked: list[tuple[str, dict]] = []

    def worker(op: str, params: dict, *, timeout: float | None = None) -> dict:
        asked.append((op, dict(params)))
        return {"duration": 6.0, "width": 320, "height": 240} if op == "probe" else {}

    monkeypatch.setattr(media, "_worker_call", worker)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    project = media.open_project(str(source))
    flagged = tmp_path / "flagged"
    flagged.mkdir()
    app = _app()
    # As on a Windows host: the write gate is on, with one flagged folder.
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    monkeypatch.setenv(gate.ENV_VAR, str(flagged))
    asked.clear()
    windows_host.on()

    opened: dict[str, tuple[int, dict]] = {}
    exported: dict[str, tuple[int, dict]] = {}
    for form in MARKER_FORMS:
        opened[form.id] = _post(app, "/media-editor/open", {"source_path": _file_on(form, "a.mp4")})
        exported[form.id] = _post(
            app, "/media-editor/export", {"project_id": project.project_id, "output_path": _file_on(form, "o.mp4")}
        )
    asked_for_marker_forms = list(asked)
    # Controls: a local relative source still opens, and a local relative output inside the flagged
    # folder still exports there.
    monkeypatch.chdir(tmp_path)
    local_open = _post(app, "/media-editor/open", {"source_path": "source.mp4"})
    monkeypatch.chdir(flagged)
    local_export = _post(app, "/media-editor/export", {"project_id": project.project_id, "output_path": "clip.mp4"})

    assert recorder.touches == [] and recorder.marker_popen == [], f"{opened}\n{exported}\n{recorder.report()}"
    for form in MARKER_FORMS:
        for (status, body), expected_status in ((opened[form.id], 400), (exported[form.id], 403)):
            assert (status, body.get("ok"), body.get("error")) == (expected_status, False, STATUS), (form.id, body)
            assert _sentence(form) in str(body.get("message") or ""), (form.id, body)
    assert asked_for_marker_forms == [], "the media worker was asked about a share"
    assert local_open[0] == 200 and local_open[1].get("ok") is True, local_open
    assert local_export[0] == 200 and local_export[1]["receipt"]["status"] == "exported", local_export
    written = [params["dst"] for _op, params in asked if "dst" in params]
    assert written and Path(written[-1]).resolve() == (flagged / "clip.mp4").resolve(), asked


def test_chat_workspace_fields_naming_a_share_are_refused_before_the_turn(
    tmp_path: Path, monkeypatch, recorder, windows_host
) -> None:
    from apps import vool_api_server
    from apps.vool_agent import VoolAgent

    agent = VoolAgent(backend_name="test-backend", device="nonlocal-test", persona_id="default")
    # The served app hands each turn to the agent the server booted with; record every turn it is handed.
    monkeypatch.setattr(vool_api_server, "_agent", agent)
    turns: list[str] = []
    real_run_agent = vool_api_server._run_agent

    def recording_run_agent(user_text: str, **kwargs: Any) -> dict[str, Any]:
        turns.append(user_text)
        return real_run_agent(user_text, **kwargs)

    monkeypatch.setattr(vool_api_server, "_run_agent", recording_run_agent)
    app = _app(agent)
    # Anything a turn resolves relative to the working directory lands in this test's folder.
    monkeypatch.chdir(tmp_path)
    windows_host.on()

    def chat(route: str, carrier: str, field: str, value: str) -> tuple[int, dict]:
        body: dict[str, Any] = {
            "messages": [{"role": "user", "content": f"hello from {carrier} {field}"}],
            "session_id": _session(),
            "stream": False,
        }
        if carrier == "body":
            body[field] = value
        else:
            body["source_context"] = {field: value}
        return _post(app, route, body)

    # Every field the route reads a workspace from, in the body and in source_context, on both routes.
    answers: dict[tuple[str, str, str, str], tuple[int, dict]] = {}
    sentences: dict[tuple[str, str, str, str], str] = {}
    for route in ("/api/chat", "/v1/chat/completions"):
        for field in CHAT_FIELDS:
            for carrier in ("body", "source_context"):
                answers[(route, carrier, field, "//attkr9/s")] = chat(route, carrier, field, "//attkr9/s")
                sentences[(route, carrier, field, "//attkr9/s")] = "is a network or device path"
    # Every form, on the first field the route reads (a programmatic caller's explicit workspace): the
    # chat workspace must be a full local path, so the partial forms are refused too.
    for form in MARKER_FORMS:
        answers[("/api/chat", "body", "workspace", form.value)] = chat("/api/chat", "body", "workspace", form.value)
        sentences[("/api/chat", "body", "workspace", form.value)] = _sentence(form)
    turns_for_marker_requests = list(turns)
    # Control: the same served chat with no workspace field still runs its turn on the Windows seam.
    control = _post(
        app, "/api/chat", {"messages": [{"role": "user", "content": "hello"}], "session_id": _session(), "stream": False}
    )

    assert recorder.touches == [] and recorder.marker_popen == [], f"{answers}\n{recorder.report()}"
    for key, (status, body) in answers.items():
        assert (status, body.get("ok"), body.get("error")) == (400, False, STATUS), (key, status, body)
        assert sentences[key] in str(body.get("message") or ""), (key, body)
    assert turns_for_marker_requests == [], "a chat turn ran for a workspace on a share"
    assert control[0] == 200, control
    assert turns == ["hello"], "the control did not reach the served turn, so the check above proves nothing"


def test_a_mode_claim_naming_a_share_is_a_mismatch_without_touch(
    tmp_path: Path, monkeypatch, recorder, windows_host
) -> None:
    monkeypatch.setattr(permission, "_bypass_grants_path", lambda: tmp_path / "authority" / "bypass_grants.json")
    permission.reset_mode_permission_state()
    app = _app()
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    session = _session()
    try:
        # The precondition the sweep reproduced: a chat bound to a real project folder ...
        status, created = _post(app, "/api/projects", {"name": "Trusted", "root": str(trusted)})
        assert status == 200, created
        status, bound = _post(app, "/api/chat/session", {"session_id": session, "project_id": created["project"]["id"]})
        assert status == 200, bound
        # ... that is asking about an ordinary workspace write, which is what grant_chat_workspace answers.
        ask = permission.decide_tool_call(
            intent="workspace.write_file",
            arguments={"path": "src/a.py", "content": "x = 1\n"},
            task_id="turn-1",
            source_context={
                "runtime_session_id": session, "session_id": session, "cancel_turn_id": "turn-1",
                "workspace": str(trusted), "workspace_root": str(trusted),
            },
        )
        assert ask.effect is permission.PermissionEffect.REQUIRE_APPROVAL, ask.reason
        windows_host.on()

        claims: dict[str, tuple[tuple[int, dict], tuple[int, dict]]] = {}
        for form in MARKER_FORMS:
            bypass = _post(
                app, "/api/mode",
                {"op": "activate_bypass", "session_id": session, "until_off": True, "workspace_root": form.value},
            )
            grant = _post(
                app, "/api/mode", {"op": "grant_chat_workspace", "session_id": session, "workspace_root": form.value}
            )
            claims[form.id] = (bypass, grant)
        # Control: the chat's real folder, claimed by a local relative spelling, still matches.
        monkeypatch.chdir(tmp_path)
        control = _post(app, "/api/mode", {"op": "grant_chat_workspace", "session_id": session, "workspace_root": "trusted"})
    finally:
        permission.reset_mode_permission_state()

    mismatch = (
        409,
        {"error": "the workspace in the request does not match this chat's actual workspace; reload the page and approve again"},
    )
    assert recorder.touches == [] and recorder.marker_popen == [], f"{claims}\n{recorder.report()}"
    assert claims == {form.id: (mismatch, mismatch) for form in MARKER_FORMS}
    assert control[0] == 200 and control[1].get("ok") is True, control
    assert Path(control[1]["scope"]["workspace_root"]).resolve() == trusted.resolve(), control
