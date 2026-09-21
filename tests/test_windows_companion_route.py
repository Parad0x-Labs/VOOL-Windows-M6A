"""The Windows pet reuses the Mac renderer without adding an effectful API door."""

from __future__ import annotations

from core.companion_world_fragment import render_desktop_companion_html
from core.web.api.runtime import RuntimeServices
from core.web.api.service import dispatch_get


def _get(path: str, *, query: dict[str, list[str]] | None = None, host: str = "127.0.0.1"):
    return dispatch_get(
        path=path,
        query=query or {},
        runtime=RuntimeServices(display_name="VOOL"),
        model_name="vool",
        client_host=host,
    )


def test_native_companion_route_reuses_the_presentation_renderer() -> None:
    response = _get("/companion")
    assert response.status == 200
    assert response.content_type.startswith("text/html")
    assert response.headers["X-Vool-Workstation-Surface"] == "companion"
    assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert response.body == render_desktop_companion_html().encode("utf-8")
    assert b"VoolDesktopCompanion" in response.body


def test_native_companion_route_refuses_remote_or_parameterized_requests() -> None:
    assert _get("/companion", host="192.0.2.1").status == 404
    assert _get("/companion", query={"caption": ["hello"]}).status == 404
    assert _get("/companion/extra").status != 200
