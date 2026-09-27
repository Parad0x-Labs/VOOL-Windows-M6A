"""Read-only path preflight for an isolated Windows lifecycle run."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping
from pathlib import Path


class PreflightRefused(RuntimeError):
    """Raised when a diagnostic writable path overlaps protected operator state."""


def _resolve(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _is_within(candidate: Path, root: Path) -> bool:
    candidate_text = os.path.normcase(os.path.abspath(str(candidate)))
    root_text = os.path.normcase(os.path.abspath(str(root)))
    try:
        return os.path.commonpath((candidate_text, root_text)) == root_text
    except ValueError:
        return False


def _default_local_app_data(env: Mapping[str, str]) -> Path:
    configured = str(env.get("LOCALAPPDATA") or "").strip()
    if configured:
        return _resolve(configured)
    return _resolve(Path.home() / "AppData" / "Local")


def resolve_lifecycle_paths(
    bundle_root: str | Path,
    run_root: str | Path,
    *,
    env: Mapping[str, str] | None = None,
    api_port: int = 11435,
    ollama_port: int = 11434,
) -> dict[str, object]:
    """Resolve lifecycle paths without creating, modifying, or probing them."""

    env_map = dict(os.environ if env is None else env)
    resolved_bundle_root = _resolve(bundle_root)
    resolved_run_root = _resolve(run_root)
    native_executable = _resolve(resolved_bundle_root / "native" / "VOOL.exe")
    vool_home = _resolve(resolved_run_root / "vool-home")
    workspace_root = _resolve(resolved_run_root / "workspace")
    webview_profile = _resolve(vool_home / "WebView2")
    temporary_root = _resolve(resolved_run_root / "temp")
    model_root = _resolve(vool_home / "models")
    writable_paths = {
        "run_root": resolved_run_root,
        "vool_home": vool_home,
        "workspace_root": workspace_root,
        "webview2_profile": webview_profile,
        "temp": temporary_root,
        "tmp": temporary_root,
        "models": model_root,
        "run_state": _resolve(vool_home / "run"),
        "logs": _resolve(vool_home / "logs"),
    }

    default_vool_home = _resolve(_default_local_app_data(env_map) / "VOOL")
    protected_roots = {"C:\\NULLA": _resolve(r"C:\NULLA"), "default_vool_home": default_vool_home}
    explicit_existing_home = str(env_map.get("VOOL_HOME") or "").strip()
    if explicit_existing_home:
        protected_roots["inherited_vool_home"] = _resolve(explicit_existing_home)

    violations = [
        f"{name}={path} overlaps {root_name}={protected_root}"
        for name, path in writable_paths.items()
        for root_name, protected_root in protected_roots.items()
        if _is_within(path, protected_root)
    ]
    if violations:
        raise PreflightRefused("; ".join(violations))

    return {
        "schema": "vool.windows.lifecycle_preflight.v1",
        "status": "resolved",
        "launch_ready": native_executable.is_file(),
        "bundle_root": str(resolved_bundle_root),
        "native_executable": str(native_executable),
        "vool_home": str(vool_home),
        "workspace_root": str(workspace_root),
        "webview2_profile": str(webview_profile),
        "temp": str(temporary_root),
        "tmp": str(temporary_root),
        "api_endpoint": f"http://127.0.0.1:{int(api_port)}",
        "ollama_endpoint": f"http://127.0.0.1:{int(ollama_port)}",
        "protected_roots": {name: str(path) for name, path in protected_roots.items()},
        "writable_paths_exist": {name: path.exists() for name, path in writable_paths.items()},
        "directories_created": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-root", required=True, type=Path)
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--api-port", type=int, default=11435)
    parser.add_argument("--ollama-port", type=int, default=11434)
    args = parser.parse_args(argv)
    try:
        report = resolve_lifecycle_paths(
            args.bundle_root,
            args.run_root,
            api_port=args.api_port,
            ollama_port=args.ollama_port,
        )
    except PreflightRefused as exc:
        print(
            json.dumps(
                {"schema": "vool.windows.lifecycle_preflight.v1", "status": "refused", "reason": str(exc)},
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
