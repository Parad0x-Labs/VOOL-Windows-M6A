"""The model build_bundle.ps1 writes into the bundle manifest is the model VOOL Auto answers with.

On the installed Windows app the chain is: build_bundle.ps1 `$Model` -> bundle_manifest.json
`selected_model` -> the bundle supervisor exports it as VOOL_OLLAMA_MODEL -> the API boot takes
that as the configured model and, when the install-profile record (config/install-profile.json)
names no other model, as a pin that installed-inventory alignment does not replace -> it is the
runtime's model_tag (healthz) and the default row of /api/tags (the chat picker's local list).
These tests cover the links that can run without booting the API: the build default, the
supervisor's export, the runtime's manifest default and the catalogue /api/tags is built from.
A clean-VM run at a7caed3 showed the old default (qwen2.5:7b) still being routed to and offered in
the picker with qwen3:8b installed and certified, so the default must reach every link.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from core.runtime_provider_defaults import default_runtime_model_tag
from core.web.api.service import _runtime_model_catalog
from installer.bundle.bundle_supervisor import BundleSupervisor

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CERTIFIED_AUTHOR = "qwen3:8b"


def _bundle_build_default_model() -> str:
    script = (PROJECT_ROOT / "installer" / "bundle" / "build_bundle.ps1").read_text(encoding="utf-8")
    match = re.search(r'\[string\]\$Model\s*=\s*"([^"]+)"', script)
    assert match, "build_bundle.ps1 no longer declares a default -Model"
    return match.group(1)


def _stage_bundle(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "bundle"
    root.mkdir()
    # The same manifest shape build_bundle.ps1 writes (PowerShell 5.1 Set-Content utf8 adds a BOM).
    (root / "bundle_manifest.json").write_text(
        json.dumps(
            {
                "schema": "vool.bundle_manifest.v1",
                "selected_model": _bundle_build_default_model(),
                "model_store": "%LOCALAPPDATA%\\VOOL\\models",
            }
        )
        + "\n",
        encoding="utf-8-sig",
    )
    home = tmp_path / "home"
    env = {"VOOL_HOME": str(home), "LOCALAPPDATA": str(tmp_path / "localappdata")}
    return root, env


def test_bundle_build_default_is_the_certified_author() -> None:
    assert _bundle_build_default_model() == CERTIFIED_AUTHOR


def test_supervisor_exports_the_bundle_default_as_the_runtime_model(tmp_path) -> None:
    root, env = _stage_bundle(tmp_path)
    supervisor = BundleSupervisor(root, env=env)
    supervisor._prepare()

    assert supervisor.env["VOOL_OLLAMA_MODEL"] == CERTIFIED_AUTHOR
    assert supervisor.env["VOOL_BUNDLE_MODEL"] == CERTIFIED_AUTHOR


def test_runtime_default_follows_the_bundle_manifest(tmp_path) -> None:
    root, env = _stage_bundle(tmp_path)
    supervisor = BundleSupervisor(root, env=env)
    supervisor._prepare()
    child_env = dict(supervisor.env)

    # With nothing exported, the runtime still resolves its default from the manifest.
    assert (
        default_runtime_model_tag(env={"VOOL_BUNDLE_MANIFEST": child_env["VOOL_BUNDLE_MANIFEST"]}) == CERTIFIED_AUTHOR
    )


def test_picker_local_list_leads_with_the_bundle_default(tmp_path) -> None:
    root, env = _stage_bundle(tmp_path)
    supervisor = BundleSupervisor(root, env=env)
    supervisor._prepare()

    catalog = _runtime_model_catalog(
        capability_snapshot={"provider_capability_truth": []},
        default_model_name=supervisor.env["VOOL_OLLAMA_MODEL"],
    )
    assert [entry["id"] for entry in catalog][:1] == [CERTIFIED_AUTHOR]
