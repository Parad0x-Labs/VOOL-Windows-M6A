"""Fail-closed launcher for an isolated Windows lifecycle run.

This is the only supported diagnostic entrypoint for launching a staged Windows bundle:

    python tools/windows_lifecycle_launcher.py check \
        --bundle-root <run-root>\bundle --run-root <run-root>
    python tools/windows_lifecycle_launcher.py launch \
        --bundle-root <run-root>\bundle --run-root <run-root>

The guard completes before any native process is created. It never reuses an inherited
``VOOL_HOME`` or Ollama model store, and it passes a scrubbed, loopback-only environment to
the native host. ``check`` performs the same validation without creating directories or starting
a process. No action is the execution-safe default: argparse rejects it before any launch code runs.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

try:
    from tools.windows_lifecycle_preflight import (
        PreflightRefused,
        _is_within,
        resolve_lifecycle_paths,
    )
except ModuleNotFoundError:  # Direct ``python tools/windows_lifecycle_launcher.py`` execution.
    from windows_lifecycle_preflight import (  # type: ignore[no-redef]
        PreflightRefused,
        _is_within,
        resolve_lifecycle_paths,
    )

API_PORT = 11435
OLLAMA_PORT = 11434
_LOCAL_ONLY_PROFILE = "local-only"

_FORCED_ENV = {
    "VOOL_WINDOWS_BUNDLE": "1",
    "VOOL_RUNTIME_MODE": "self-contained",
    "VOOL_INSTALL_PROFILE": _LOCAL_ONLY_PROFILE,
    "VOOL_LOCAL_MODELS_ENABLED": "1",
    "VOOL_PUBLIC_HIVE_ENABLED": "0",
    "VOOL_DISABLE_MESH_DAEMON": "1",
    "VOOL_DISABLE_WEB": "1",
    "VOOL_NO_WEB": "1",
    "VOOL_ENABLE_WEB": "0",
    "VOOL_NATIVE_REQUIRE_OWNED_RUNTIME": "0",
    "VOOL_NATIVE_ATTACH_ONLY": "1",
    "VOOL_NATIVE_API_URL": f"http://127.0.0.1:{API_PORT}",
    "VOOL_RAW_OLLAMA_API_URL": f"http://127.0.0.1:{OLLAMA_PORT}",
    "VOOL_OLLAMA_URL": f"http://127.0.0.1:{OLLAMA_PORT}",
    "OLLAMA_HOST": f"127.0.0.1:{OLLAMA_PORT}",
    "OLLAMA_NO_CLOUD": "true",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONNOUSERSITE": "1",
    "NO_PROXY": "127.0.0.1,localhost",
    "no_proxy": "127.0.0.1,localhost",
}
_SCRUB_EXACT = frozenset(
    {
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AZURE_OPENAI_API_KEY",
        "AZURE_API_KEY",
        "GITHUB_TOKEN",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "HF_TOKEN",
        "HUGGINGFACEHUB_API_TOKEN",
        "OPENAI_API_KEY",
        "OLLAMA_API_KEY",
        "VOOL_KEY_PASSPHRASE",
        "VOOL_MEET_AUTH_TOKEN",
        "VOOL_REMOTE_API_KEY",
    }
)
_SCRUB_MARKERS = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_PASSPHRASE")
_SCRUB_ENV_NAMES = frozenset(
    {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "VOOL_CUSTOM_BASE_URL",
        "VOOL_REMOTE_BASE_URL",
        "VOOL_OLLAMA_CHAT_URL",
        "VOOL_OLLAMA_PS_URL",
        "VOOL_OLLAMA_TAGS_URL",
        "VOOL_MEET_SEED_URLS",
    }
)
_REQUIRED_BUNDLE_MEMBERS = (
    Path("native") / "VOOL.exe",
    Path("bundle_manifest.json"),
    Path("app") / "apps" / "vool_api_server.py",
    Path("bundle_supervisor.py"),
    Path("vool_window.py"),
    Path("python") / "python.exe",
    Path("python") / "pythonw.exe",
    Path("ollama") / "ollama.exe",
    Path("ollama") / "lib" / "ollama" / "llama-server.exe",
)


class LaunchRefused(RuntimeError):
    """Raised before process creation when isolation cannot be proven."""


@dataclass(frozen=True)
class LaunchSpec:
    bundle_root: Path
    run_root: Path
    native_executable: Path
    paths: dict[str, str]
    environment: dict[str, str]
    scrubbed_environment_names: tuple[str, ...]


def _resolve(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _is_credential_name(name: str) -> bool:
    upper = str(name or "").upper()
    return upper in _SCRUB_EXACT or any(marker in upper for marker in _SCRUB_MARKERS)


def _must_scrub_environment_name(name: str) -> bool:
    upper = str(name or "").upper()
    return _is_credential_name(upper) or upper.startswith("NULLA_") or upper in {
        value.upper() for value in _SCRUB_ENV_NAMES
    }


def _scrubbed_environment(source: Mapping[str, str]) -> tuple[dict[str, str], tuple[str, ...]]:
    clean: dict[str, str] = {}
    scrubbed: list[str] = []
    for name, value in source.items():
        if _must_scrub_environment_name(name):
            scrubbed.append(str(name))
            continue
        clean[str(name)] = str(value)
    return clean, tuple(sorted(set(scrubbed), key=str.casefold))


def _ports_are_free(ports: tuple[int, ...] = (API_PORT, OLLAMA_PORT)) -> bool:
    sockets: list[socket.socket] = []
    try:
        for port in ports:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sockets.append(sock)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        for sock in sockets:
            sock.close()


def _path_map(preflight: Mapping[str, object]) -> dict[str, str]:
    return {
        name: str(preflight[name])
        for name in (
            "bundle_root",
            "native_executable",
            "vool_home",
            "workspace_root",
            "webview2_profile",
            "temp",
            "tmp",
        )
    } | {"models": str(Path(str(preflight["vool_home"])) / "models")}


def build_launch_spec(
    bundle_root: str | Path,
    run_root: str | Path,
    *,
    env: Mapping[str, str] | None = None,
    port_probe: Callable[[], bool] = _ports_are_free,
) -> LaunchSpec:
    """Validate the complete isolation contract without creating paths or processes."""

    source_env = dict(os.environ if env is None else env)
    resolved_bundle = _resolve(bundle_root)
    resolved_run = _resolve(run_root)
    preflight_env = dict(source_env)
    preflight_env.pop("VOOL_HOME", None)
    preflight_env.pop("OLLAMA_MODELS", None)
    try:
        preflight = resolve_lifecycle_paths(
            resolved_bundle,
            resolved_run,
            env=preflight_env,
            api_port=API_PORT,
            ollama_port=OLLAMA_PORT,
        )
    except PreflightRefused as exc:
        raise LaunchRefused(str(exc)) from exc

    if not _is_within(resolved_bundle, resolved_run):
        raise LaunchRefused("bundle root must be inside the disposable run root")
    source_root = _resolve(Path(__file__).parents[1])
    if _is_within(resolved_run, source_root) or _is_within(resolved_bundle, source_root):
        raise LaunchRefused("lifecycle bundle and run root must be outside the source checkout")
    if not bool(preflight["launch_ready"]):
        raise LaunchRefused(f"native host missing: {preflight['native_executable']}")
    missing = [str(member) for member in _REQUIRED_BUNDLE_MEMBERS if not (resolved_bundle / member).is_file()]
    if missing:
        raise LaunchRefused("staged bundle is incomplete: " + ", ".join(missing))
    if not port_probe():
        raise LaunchRefused(f"loopback ports {OLLAMA_PORT} and {API_PORT} are not both free")

    paths = _path_map(preflight)
    paths.update(
        {
            "run_root": str(resolved_run),
            "localappdata": str(resolved_run / "localappdata"),
            "appdata": str(resolved_run / "appdata"),
            "user_profile": str(resolved_run / "user-profile"),
        }
    )
    protected = tuple(_resolve(value) for value in preflight["protected_roots"].values())
    for name, raw_path in paths.items():
        path = _resolve(raw_path)
        if not _is_within(path, resolved_run):
            raise LaunchRefused(f"{name} is outside the disposable run root: {path}")
        if any(_is_within(path, root) for root in protected):
            raise LaunchRefused(f"{name} overlaps protected operator state: {path}")

    clean_env, scrubbed = _scrubbed_environment(source_env)
    clean_env.update(
        {
            "VOOL_HOME": paths["vool_home"],
            "VOOL_WORKSPACE_ROOT": paths["workspace_root"],
            "VOOL_PROJECT_ROOT": str(resolved_bundle / "app"),
            "VOOL_BUNDLE_ROOT": paths["bundle_root"],
            "VOOL_BUNDLE_MANIFEST": str(resolved_bundle / "bundle_manifest.json"),
            "OLLAMA_MODELS": paths["models"],
            "TEMP": paths["temp"],
            "TMP": paths["tmp"],
            "LOCALAPPDATA": paths["localappdata"],
            "APPDATA": paths["appdata"],
            "USERPROFILE": paths["user_profile"],
            "HOME": paths["user_profile"],
            "PYTHONPATH": str(resolved_bundle / "app"),
        }
    )
    clean_env.update(_FORCED_ENV)

    # These values are deliberately checked after the merge so a future edit cannot silently
    # change the policy while retaining the same launcher surface.
    required_values = {
        "VOOL_INSTALL_PROFILE": _LOCAL_ONLY_PROFILE,
        "VOOL_LOCAL_MODELS_ENABLED": "1",
        "VOOL_PUBLIC_HIVE_ENABLED": "0",
        "VOOL_ENABLE_WEB": "0",
        "OLLAMA_NO_CLOUD": "true",
        "OLLAMA_HOST": f"127.0.0.1:{OLLAMA_PORT}",
    }
    for name, expected in required_values.items():
        if clean_env.get(name) != expected:
            raise LaunchRefused(f"isolated policy value {name} is not {expected!r}")
    if any(_is_credential_name(name) for name in clean_env):
        raise LaunchRefused("credential-bearing environment variable survived scrubbing")

    return LaunchSpec(
        bundle_root=resolved_bundle,
        run_root=resolved_run,
        native_executable=_resolve(preflight["native_executable"]),
        paths=paths,
        environment=clean_env,
        scrubbed_environment_names=scrubbed,
    )


def _prepare_disposable_directories(spec: LaunchSpec) -> None:
    for name in ("vool_home", "workspace_root", "webview2_profile", "temp", "models"):
        Path(spec.paths[name]).mkdir(parents=True, exist_ok=True)
    for name in ("localappdata", "appdata", "user-profile"):
        (spec.run_root / name).mkdir(parents=True, exist_ok=True)


def launch(
    bundle_root: str | Path,
    run_root: str | Path,
    *,
    env: Mapping[str, str] | None = None,
    port_probe: Callable[[], bool] = _ports_are_free,
    popen_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
) -> tuple[LaunchSpec, subprocess.Popen]:
    """Validate first, prepare only disposable directories, then create the native process."""

    spec = build_launch_spec(bundle_root, run_root, env=env, port_probe=port_probe)
    _prepare_disposable_directories(spec)
    process = popen_factory(
        [str(spec.native_executable)],
        cwd=str(spec.bundle_root),
        env=spec.environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    return spec, process


def _report(spec: LaunchSpec) -> dict[str, object]:
    return {
        "schema": "vool.windows.lifecycle.launch_guard.v1",
        "status": "verified",
        "launch_ready": True,
        "bundle_root": str(spec.bundle_root),
        "native_executable": str(spec.native_executable),
        "paths": spec.paths,
        "api_endpoint": f"http://127.0.0.1:{API_PORT}",
        "ollama_endpoint": f"http://127.0.0.1:{OLLAMA_PORT}",
        "local_only": True,
        "remote_fallback_disabled": True,
        "ollama_no_cloud": True,
        "scrubbed_environment_names": list(spec.scrubbed_environment_names),
        "environment_names": sorted(spec.environment, key=str.casefold),
        "directories_created": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    for action, help_text in (
        ("check", "verify the isolated launch guard without starting VOOL"),
        ("launch", "verify the isolated launch guard, then start VOOL"),
    ):
        action_parser = actions.add_parser(action, help=help_text)
        action_parser.add_argument("--bundle-root", required=True, type=Path)
        action_parser.add_argument("--run-root", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "check":
            spec = build_launch_spec(args.bundle_root, args.run_root)
            print(json.dumps(_report(spec), indent=2, sort_keys=True))
            return 0
        if args.action != "launch":
            parser.error("an explicit check or launch action is required")
        spec, process = launch(args.bundle_root, args.run_root)
    except LaunchRefused as exc:
        print(json.dumps({"schema": "vool.windows.lifecycle.launch_guard.v1", "status": "refused", "reason": str(exc)}))
        return 2
    print(json.dumps({**_report(spec), "pid": process.pid, "directories_created": True}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
