"""Ollama cloud models: VOOL never pulls, lists, certifies, routes to or runs one.

Capability test (fresh Windows 11 VM, build 828b8bda, bundled Ollama 0.35.1): with the bundled
``ollama serve`` started with ``OLLAMA_NO_CLOUD=1`` (installer/bundle/bundle_supervisor.py), a
hand-run ``ollama pull gpt-oss:20b-cloud`` still succeeded. Serve fetched a cloud stub from
registry.ollama.ai: a manifest whose config names ``remote_host`` (https://ollama.com) and
``remote_model``, with no weights. VOOL then listed it. ``ollama run`` of the stub was refused
("ollama cloud is disabled"), so ``OLLAMA_NO_CLOUD`` blocks running but not pulling.

VOOL's cloud lanes are its own providers, behind its own consent; it never uses Ollama's. So every
VOOL surface treats an Ollama cloud model as absent, and refuses one by name:

* a name whose tag is ``cloud`` or ends in ``-cloud`` (``gpt-oss:20b-cloud``, ``glm-4.6:cloud``),
  or whose model part ends in ``-cloud`` with no tag;
* a model whose Ollama listing or ``/api/show`` row carries ``remote_host`` or ``remote_model``;
* a model whose manifest in Ollama's model store (read from disk, no request to Ollama) carries
  them, or that cannot be read (an unknown answer counts as cloud).

Nothing here contacts Ollama or the network. Refusals are plain sentences.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

REASON = "ollama_cloud_model_refused"

_REMOTE_KEYS = ("remote_host", "remote_model")
_NAME_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_DEFAULT_HOST = "registry.ollama.ai"
_DEFAULT_NAMESPACE = "library"
_DEFAULT_TAG = "latest"


class CloudModelRefused(ValueError):
    """A request named an Ollama cloud model. ``str()`` is the sentence the user reads."""

    def __init__(self, model: str) -> None:
        self.model = str(model or "").strip()
        super().__init__(refusal_text(self.model))


def refusal_text(model: str) -> str:
    name = str(model or "").strip() or "That model"
    return (
        f"{name} is an Ollama cloud model: it runs on ollama.com, not on this computer. "
        "VOOL does not download, list or run Ollama cloud models."
    )


def _split_name(model: str) -> tuple[str, str] | None:
    """(path part, tag) of an Ollama model name, digest dropped; None when there is no name."""
    clean = str(model or "").strip()
    if not clean:
        return None
    clean = clean.split("@", 1)[0]
    last_slash = clean.rfind("/")
    colon = clean.rfind(":")
    if colon > last_slash:
        return clean[:colon], clean[colon + 1 :]
    return clean, ""


def is_cloud_model_name(model: Any) -> bool:
    """Whether the name alone says Ollama cloud. Matching ignores case and surrounding space."""
    parts = _split_name(str(model or ""))
    if parts is None:
        return False
    path, tag = parts[0].lower(), parts[1].lower()
    if tag == "cloud" or tag.endswith("-cloud"):
        return True
    return not tag and path.rsplit("/", 1)[-1].endswith("-cloud")


def entry_is_remote(entry: Any) -> bool:
    """Whether an Ollama ``/api/tags`` row, ``/api/show`` reply or model config names a remote host."""
    if not isinstance(entry, Mapping):
        return False
    for scope in (entry, entry.get("details")):
        if isinstance(scope, Mapping) and any(str(scope.get(key) or "").strip() for key in _REMOTE_KEYS):
            return True
    return False


def _model_stores(env: Mapping[str, str] | None) -> list[Path]:
    """The store ``ollama serve`` uses: OLLAMA_MODELS (the bundle supervisor sets it for both Ollama
    and VOOL), else Ollama's default ``~/.ollama/models``."""
    env_map = os.environ if env is None else env
    override = str(env_map.get("OLLAMA_MODELS") or "").strip()
    if not override and env is not None:
        override = str(os.environ.get("OLLAMA_MODELS") or "").strip()
    if override:
        return [Path(override).expanduser()]
    try:
        return [Path.home() / ".ollama" / "models"]
    except (RuntimeError, OSError):
        return []


def _manifest_relpath(model: str) -> Path | None:
    """manifests/<host>/<namespace>/<model>/<tag>, as Ollama lays its store out; None if unparseable."""
    parts = _split_name(model)
    if parts is None:
        return None
    path, tag = parts
    segments = path.split("/")
    if len(segments) == 1:
        segments = [_DEFAULT_HOST, _DEFAULT_NAMESPACE, *segments]
    elif len(segments) == 2:
        segments = [_DEFAULT_HOST, *segments]
    tag = tag or _DEFAULT_TAG
    if len(segments) != 3 or not all(_NAME_PART.match(item) for item in (*segments, tag)):
        return None
    return Path("manifests", *segments, tag)


def _read_json(path: Path) -> Any:
    with path.open("rb") as handle:
        return json.loads(handle.read(1_048_576).decode("utf-8"))


def manifest_reports_remote(model: str, *, env: Mapping[str, str] | None = None) -> bool:
    """Whether Ollama's own store holds this model as a cloud stub. Read from disk only.

    No manifest anywhere: False (there is no local stub). A name that cannot be laid out as an
    Ollama store path, a manifest or config that cannot be read, or a config that names
    ``remote_host`` / ``remote_model``: True.
    """
    relpath = _manifest_relpath(model)
    if relpath is None:
        return True
    for store in _model_stores(env):
        candidates = [store / relpath]
        lowered = store / Path(*(part.lower() for part in relpath.parts))
        if lowered != candidates[0]:
            candidates.append(lowered)
        for manifest_path in candidates:
            try:
                if not manifest_path.is_file():
                    continue
                manifest = _read_json(manifest_path)
                if not isinstance(manifest, Mapping) or entry_is_remote(manifest):
                    return True
                config = manifest.get("config")
                digest = str(config.get("digest") or "") if isinstance(config, Mapping) else ""
                if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
                    return True
                blob = _read_json(store / "blobs" / digest.replace(":", "-"))
                if not isinstance(blob, Mapping) or entry_is_remote(blob):
                    return True
            except (OSError, ValueError, UnicodeDecodeError):
                return True
    return False


def is_cloud_model(model: Any, *, entry: Any = None, env: Mapping[str, str] | None = None) -> bool:
    """Name, then the row Ollama gave (if any), then the store on disk."""
    name = str(model or "").strip()
    if not name:
        return False
    if is_cloud_model_name(name) or entry_is_remote(entry):
        return True
    return manifest_reports_remote(name, env=env)


def refuse_cloud_model(model: Any, *, entry: Any = None, env: Mapping[str, str] | None = None) -> None:
    """Raise ``CloudModelRefused`` for an Ollama cloud model; return for anything else."""
    if is_cloud_model(model, entry=entry, env=env):
        raise CloudModelRefused(str(model or ""))


def refuse_cloud_model_name(model: Any) -> None:
    """The name-only check, for hot paths that must not read the disk (each model call)."""
    if is_cloud_model_name(model):
        raise CloudModelRefused(str(model or ""))


def local_tag_rows(models: Any, *, env: Mapping[str, str] | None = None) -> list[Any]:
    """An Ollama ``/api/tags`` ``models`` list without its cloud rows (by name, row or store)."""
    if not isinstance(models, list):
        return models
    kept: list[Any] = []
    for row in models:
        if isinstance(row, Mapping):
            name = str(row.get("name") or row.get("model") or "").strip()
            if is_cloud_model(name, entry=row, env=env):
                continue
        kept.append(row)
    return kept


def manifest_is_ollama_cloud(manifest: Any) -> bool:
    """Whether a registry manifest names an Ollama cloud model (by name or recorded remote host)."""
    name = str(getattr(manifest, "model_name", "") or "").strip()
    metadata = getattr(manifest, "metadata", None) or {}
    runtime_config = getattr(manifest, "runtime_config", None) or {}
    family = str(metadata.get("runtime_family") or "").strip().lower() if isinstance(metadata, Mapping) else ""
    provider = str(getattr(manifest, "provider_name", "") or "").strip().lower()
    ollama = family == "ollama" or provider.startswith("ollama")
    if not ollama:
        try:
            from core.local_model_policy import manifest_is_local

            ollama = bool(manifest_is_local(manifest))
        except Exception:
            ollama = False
    if not ollama:
        return False
    return is_cloud_model_name(name) or entry_is_remote(metadata) or entry_is_remote(runtime_config)


def without_ollama_cloud_manifests(manifests: Iterable[Any]) -> list[Any]:
    return [manifest for manifest in manifests if not manifest_is_ollama_cloud(manifest)]


__all__ = [
    "REASON",
    "CloudModelRefused",
    "entry_is_remote",
    "is_cloud_model",
    "is_cloud_model_name",
    "local_tag_rows",
    "manifest_is_ollama_cloud",
    "manifest_reports_remote",
    "refusal_text",
    "refuse_cloud_model",
    "refuse_cloud_model_name",
    "without_ollama_cloud_manifests",
]
