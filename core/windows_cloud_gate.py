"""Windows: no contact with a cloud model provider before the owner opts in.

Capability test (fresh Windows 11 VM, no key stored, cloud never turned on): the chat turn "use a
cloud model to say hi" resolved openrouter.ai and downloaded its model catalog (about 883 KB),
and VOOL wrote ``openrouter_models_cache.json``. The ship bar says no network before opt-in.

The rule, on Windows only: VOOL sends no request to a cloud model provider (the OpenRouter
catalog, a provider's model list, its prices, a health or discovery probe, a model call) unless
both hold:

* a cloud key is stored: a provider's key slot (``llm.cloud.<provider>``, the slots
  ``core.cloud_providers.PROVIDERS`` names) is in the credential index on disk. A key in an
  environment variable is not a stored key, and the endpoint slots (``llm.cloud.usepod_origin``,
  ``llm.cloud.custom_base_url``) are not keys;
* the owner has turned cloud on: the cloud routing consent in ``cloud_escalation.json``
  (``free_cloud_enabled``, or a ``mode`` of ``ask`` or ``auto``). It is the consent
  ``core.cloud_transport`` already reads before a model call. Saving an OpenRouter key in
  Settings sets ``free_cloud_enabled`` (``registry_authorities.bind_saved_cloud_key``);
  ``cloud ask`` / ``cloud auto``, ``cloud model auto`` and the Settings "Auto fallback" picker set it too.

Without both, every gated caller answers locally, with ``OFF_TEXT``, and opens no socket.

One request is not gated: the key check the owner starts by pressing Verify on a key they pasted
(``owner_key_check``). It sends that key to the provider's own verification endpoint and nothing
else, and it is how a key gets stored at all.

Scope (0.7.1): behind the same seam as the Windows Ollama cloud-model refusal,
``core.ollama_cloud_models.active()`` (``CLOUD_REFUSAL_ACTIVE``). Off Windows nothing changes.
"""
from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from contextvars import ContextVar
from urllib.parse import urlsplit

from core.remote_fetch_policy import RemoteFetchRefusedError

OFF_TEXT = "Cloud models are off. Add your own key in Settings to use one."
REASON = "cloud_models_off_before_opt_in"

_OWNER_KEY_CHECK: ContextVar[bool] = ContextVar("vool_windows_owner_key_check", default=False)


class CloudContactRefused(RemoteFetchRefusedError):
    """A cloud provider request was refused before any socket. ``str()`` is ``OFF_TEXT``.

    A ``RemoteFetchRefusedError``, so every caller that already handles a refused fetch handles it."""

    def __init__(self, what: str = "") -> None:
        self.what = str(what or "")
        super().__init__(OFF_TEXT)


def active() -> bool:
    """Whether this process enforces the rule: the Windows cloud-model refusal's own seam."""
    from core.ollama_cloud_models import active as _cloud_refusal_active

    return _cloud_refusal_active()


def _provider_slots() -> frozenset[str]:
    try:
        from core.cloud_providers import PROVIDERS

        return frozenset(config.credential_slot for config in PROVIDERS.values())
    except Exception:
        return frozenset({"llm.cloud.openrouter"})


def cloud_key_stored() -> bool:
    """Whether a provider key slot is in the credential index on disk. Names only, no secret."""
    from core.runtime_paths import data_path

    slots = _provider_slots()
    for filename in ("credentials.meta.json", "credentials.enc.json"):
        try:
            path = data_path(filename)
            if not path.exists():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict) and any(str(name) in slots for name in data):
            return True
    return False


def owner_turned_cloud_on() -> bool:
    """The cloud routing consent: free cloud on, or escalation ``ask`` / ``auto``."""
    try:
        from core.cloud_escalation_policy import MODE_ASK, MODE_AUTO, load_policy

        policy = load_policy()
        return bool(policy.free_cloud_enabled) or policy.mode in {MODE_ASK, MODE_AUTO}
    except Exception:
        return False


def cloud_contact_allowed() -> bool:
    """True off Windows; on Windows only with a stored key AND cloud turned on."""
    if not active():
        return True
    return cloud_key_stored() and owner_turned_cloud_on()


def cloud_off() -> bool:
    """The rule is in force and cloud is not opted in: callers answer ``OFF_TEXT``."""
    return not cloud_contact_allowed()


def refuse_cloud_contact(what: str = "") -> None:
    """Raise ``CloudContactRefused`` when the rule refuses; return otherwise."""
    if cloud_off():
        raise CloudContactRefused(what)


@contextlib.contextmanager
def owner_key_check() -> Iterator[None]:
    """Scope of the owner's own Verify press on a pasted key (see the module docstring)."""
    token = _OWNER_KEY_CHECK.set(True)
    try:
        yield
    finally:
        _OWNER_KEY_CHECK.reset(token)


def _cloud_hosts() -> frozenset[str]:
    hosts = {"openrouter.ai"}
    try:
        from core.cloud_providers import PROVIDERS

        for config in PROVIDERS.values():
            host = str(urlsplit(str(config.base_url or "")).hostname or "").lower()
            if host:
                hosts.add(host)
    except Exception:
        pass
    try:
        from core.usepod.discovery import configured_origin

        host = str(urlsplit(configured_origin()).hostname or "").lower()
        if host:
            hosts.add(host)
    except Exception:
        pass
    return frozenset(hosts)


def _custom_endpoint_host() -> str:
    """The owner's custom (BYOK) endpoint host, unless it is on this machine or the LAN."""
    try:
        from core.auto_local_only_mode import endpoint_is_local
        from core.cloud_providers import custom_base_url

        url = str(custom_base_url() or "").strip()
        if not url or endpoint_is_local(url):
            return ""
        return str(urlsplit(url).hostname or "").lower()
    except Exception:
        return ""


def is_cloud_provider_request(url: str, provider_id: str = "") -> bool:
    """Whether a request goes to a cloud model provider, by its host: a provider's base URL host,
    openrouter.ai, the UsePod origin, or the owner's custom endpoint when that is not local.

    The declared ``provider_id`` is not used: other services share ids with model providers (the
    calendar adapter declares ``google``)."""
    host = str(urlsplit(str(url or "")).hostname or "").lower().rstrip(".")
    if not host:
        return False
    known_hosts = set(_cloud_hosts())
    custom = _custom_endpoint_host()
    if custom:
        known_hosts.add(custom)
    return any(host == known or host.endswith("." + known) for known in known_hosts)


def door_refusal(url: str, provider_id: str = "") -> CloudContactRefused | None:
    """The outbound door's backstop: the refusal to raise for this request, or None."""
    if not active() or _OWNER_KEY_CHECK.get():
        return None
    if not is_cloud_provider_request(url, provider_id):
        return None
    if cloud_contact_allowed():
        return None
    return CloudContactRefused(str(url or ""))


__all__ = [
    "OFF_TEXT",
    "REASON",
    "CloudContactRefused",
    "active",
    "cloud_contact_allowed",
    "cloud_key_stored",
    "cloud_off",
    "door_refusal",
    "is_cloud_provider_request",
    "owner_key_check",
    "owner_turned_cloud_on",
    "refuse_cloud_contact",
]
