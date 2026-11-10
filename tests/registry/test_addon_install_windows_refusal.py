"""Windows 0.7.1 starts no plugin or MCP server. When the add-on door's install meets that refusal,
the owner reads it in its own words (HTTP 400, error "refused_on_windows", the exact sentence), not
the generic "could not finish, check storage access" answer meant for real storage faults. Every
other failure, and every failure off Windows, still gets that generic answer unchanged."""
import json
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from core import plugin_executor, plugin_lifecycle
from core.mcp_client import MCPRefusedOnWindows
from core.plugin_executor import ConfinementUnavailableError
from core.plugin_lifecycle import LifecycleError
from core.registry.client import Client
from core.registry.trust import TrustedCatalog
from core.web.api.addon_api import handle_addon_post
from registry_service.api import create_app
from registry_service.auth import Auth
from registry_service.store import Store
from sandbox import job_runner
from tests import _windows_refusals as windows_refusals
from tests.registry.audit_helpers import package
from tests.registry.helpers import NOW, TARGET, catalog_fixture, trust_fixture

PLUGIN = plugin_executor.WINDOWS_PLUGIN_MCP_REFUSAL
REFUSED = {"ok": False, "error": plugin_executor.WINDOWS_REFUSED_STATUS, "message": PLUGIN}
GENERIC = {"ok": False, "error": "addon_operation_failed",
           "message": "The add-on operation could not finish. Check storage access and review its status before retrying."}
HEADERS = {"content-type": "application/json"}


def _windows(monkeypatch, on: bool) -> None:
    """The plugin refusal's own seam (plugin_executor.windows_plugin_mcp_refusal asks both)."""
    monkeypatch.setattr(plugin_executor, "_WINDOWS_REFUSES_PLUGINS_AND_MCP", on)
    monkeypatch.setattr(job_runner, "_is_windows_platform", lambda: on)


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """A served registry holding one pack with a runnable tool, reached the way the add-ons page does."""
    from core.registry import client as native_client

    runtime_home = tmp_path / "runtime"
    monkeypatch.setenv("VOOL_HOME", str(runtime_home))
    monkeypatch.setenv("VOOL_PLUGIN_LIFECYCLE_PATH", str(runtime_home / "plugin_lifecycle.json"))
    monkeypatch.setenv("VOOL_PLUGINS_DIR", str(tmp_path / "plugin-home"))
    monkeypatch.setenv("VOOL_BUNDLED_PLUGINS_DIR", str(tmp_path / "no-bundled"))
    monkeypatch.setenv("VOOL_NATIVE_SKILLS_DIR", str(tmp_path / "no-native"))
    raw, _, _ = package(tmp_path, tools=True, permissions=("external_messages",))
    policy, keys, root = trust_fixture()
    server_trust = TrustedCatalog(root, tmp_path / "server/trust", now=NOW)
    server_trust.accept(catalog_fixture(raw, policy, keys), now=NOW)
    store = Store(tmp_path / "server/accounts")
    store.publish(server_trust, "alice/echo", "1.0.0", TARGET, raw, now=NOW)
    service = TestClient(create_app(store, Auth(store, "https://registry.test", []), server_trust, TARGET, clock=lambda: NOW),
                         base_url="https://registry.test")

    def request(url, *, limit):
        response = service.get(url)
        assert response.status_code == 200 and len(response.content) <= limit
        return response.content

    monkeypatch.setattr(native_client, "time", SimpleNamespace(time=lambda: NOW))
    monkeypatch.setattr(native_client, "configured_client", lambda: Client(root, TARGET, origin="https://registry.test", request=request, now=NOW))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    try:
        yield SimpleNamespace(workspace=workspace, runtime_home=runtime_home, plugin_home=tmp_path / "plugin-home")
    finally:
        service.close()
        server_trust.close()


def _post(body: dict, workspace) -> tuple[int, dict]:
    from core.web.api.runtime import RuntimeServices
    from core.web.api.service import dispatch_post

    result = dispatch_post(path="/api/addons", body=body, headers=HEADERS, runtime=RuntimeServices(display_name="VOOL"),
                           model_name="fixture", workspace_root_provider=lambda: str(workspace), client_host="127.0.0.1")
    return result.status, json.loads(result.body)


def _prepare(workspace) -> dict:
    status, refreshed = _post({"action": "registry_refresh", "approved": True}, workspace)
    assert status == 200 and [row["id"] for row in refreshed["entries"]] == ["alice/echo"], refreshed
    status, review = _post({"action": "registry_prepare", "id": "alice/echo", "version": "1.0.0", "approved": True}, workspace)
    assert status == 200 and review["status"] == "review", review
    return review


def test_on_windows_the_served_registry_install_answers_the_plugin_refusal_in_its_own_words(registry, monkeypatch) -> None:
    _windows(monkeypatch, True)
    review = _prepare(registry.workspace)
    with windows_refusals.watch_plugins(registry.workspace) as seen:
        status, answer = _post({"action": "registry_install", "id": "alice/echo", "version": "1.0.0",
                                "review_id": review["review_id"], "accepted": True, "enable": True}, registry.workspace)
    assert windows_refusals.refused_exactly(seen, (status, 400), (answer, REFUSED), (answer["message"], PLUGIN))
    # The install rolled back as before: no pack is left installed, its record is uninstalled and
    # not enabled, and nothing is available.
    assert list((registry.plugin_home / "plugins").iterdir()) == []
    assert [(record.stage, record.enabled) for record in plugin_lifecycle.records()] == [("uninstalled", False)]
    assert not any(plugin_lifecycle.is_available(record.plugin_id) for record in plugin_lifecycle.records())


@pytest.mark.parametrize("refusal", [LifecycleError, ConfinementUnavailableError, MCPRefusedOnWindows])
def test_on_windows_each_plugin_refusal_type_reaches_the_owner_in_its_own_words(monkeypatch, refusal) -> None:
    _windows(monkeypatch, True)

    def refuse(_action, _body):
        raise refusal(PLUGIN)

    monkeypatch.setattr("core.registry.client.handle", refuse)
    assert handle_addon_post({"action": "registry_install"}, HEADERS, "127.0.0.1") == (400, REFUSED)


@pytest.mark.parametrize("refusal", [LifecycleError, ConfinementUnavailableError, MCPRefusedOnWindows])
def test_on_windows_another_failure_of_the_same_type_keeps_the_generic_answer(monkeypatch, refusal) -> None:
    _windows(monkeypatch, True)

    def fail(_action, _body):
        raise refusal("pack: complete package verification is required before enabling")

    monkeypatch.setattr("core.registry.client.handle", fail)
    assert handle_addon_post({"action": "registry_install"}, HEADERS, "127.0.0.1") == (500, GENERIC)


@pytest.mark.parametrize("refusal", [LifecycleError, ConfinementUnavailableError, MCPRefusedOnWindows, RuntimeError])
def test_off_windows_every_install_failure_keeps_the_generic_answer(monkeypatch, refusal) -> None:
    _windows(monkeypatch, False)

    def fail(_action, _body):
        raise refusal(PLUGIN)

    monkeypatch.setattr("core.registry.client.handle", fail)
    assert handle_addon_post({"action": "registry_install"}, HEADERS, "127.0.0.1") == (500, GENERIC)
