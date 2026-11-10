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
  A cloud model the owner pinned by name counts as well (``owner_pin``): a confirmed paid pin turns
  the free lane off on purpose, and must not close the gate on the model it just chose. Only the
  owner's own pin counts, never one a council seat, an agent turn or a model wrote. ``cloud off``
  clears the pin with the free lane.

Without both, every gated caller answers locally, with ``OFF_TEXT``, and opens no socket.

One request is not gated: the owner's Verify in Settings on a key they pasted (the
``/api/intake/verify`` route, owner-local only, opens ``owner_key_check``). It sends that key to the
selected provider's verification host and nothing else, and it is how a key gets stored at all.
The verifier itself grants nothing: called from anywhere else (a tool, a probe, a scan) it is refused.

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

#: The chat header texts every UI locale shows while cloud is off: nothing reads as free cloud, and
#: Auto's free-only rule is still stated. ``core.i18n.page_bundle`` puts them in the page's i18n
#: bundle; ``core.vool_chat_page`` puts them in the served template.
CLOUD_OFF_UI_TEXTS = {
    "header.run_local_first_hint": "cloud models off",
    "header.model_auto_hint": "local first",
    "header.model_auto_explainer": "Starts local. " + OFF_TEXT + " Auto never selects a paid model.",
    "header.model_connect_note": OFF_TEXT,
}

#: The one host the owner's Settings Verify may reach, while that route runs ("" = none).
_OWNER_KEY_CHECK: ContextVar[str] = ContextVar("vool_windows_owner_key_check", default="")


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
    """The cloud routing consent: free cloud on, escalation ``ask`` / ``auto``, or a cloud model the
    owner pinned by name.

    The pin counts because it is the owner's own act naming a cloud model (a paid one only with
    their spend confirmation), and because that same act turns the free lane off on purpose
    (``cloud_model_control.set_cloud_model``): without it, confirming a paid pin in Settings would
    close the gate on the model just chosen. Only the owner's pin counts: ``policy.model`` must be
    the pin ``record_owner_pin`` noted for an owner write. A pin written by anything else (a
    council's seat pin, any other writer of the policy file) is not consent. ``cloud off`` clears
    both (``fast_command_surface.maybe_handle_cloud_command``)."""
    try:
        from core.cloud_escalation_policy import MODE_ASK, MODE_AUTO, load_policy

        policy = load_policy()
        if bool(policy.free_cloud_enabled) or policy.mode in {MODE_ASK, MODE_AUTO}:
            return True
        pinned = _canonical_pin(policy.model)
        return bool(pinned) and pinned == owner_pin()
    except Exception:
        return False


_OWNER_PIN_FILE = "windows_cloud_owner_pin.json"


def _canonical_pin(model: str) -> str:
    pin = str(model or "").strip().lower()
    return "" if pin in {"", "auto", "default", "reset"} else pin


def owner_pin() -> str:
    """The cloud model the owner last pinned themselves ("" = none). Names only, no secret."""
    try:
        from core.runtime_paths import data_path

        path = data_path(_OWNER_PIN_FILE)
        if not path.exists():
            return ""
        data = json.loads(path.read_text(encoding="utf-8"))
        return _canonical_pin(data.get("model", "")) if isinstance(data, dict) else ""
    except Exception:
        return ""


def record_owner_pin(model: str) -> None:
    """Note ``model`` as the owner's own pin ("" clears it). Called only by the owner's pin write:
    ``set_cloud_model`` from the Settings route or a command the owner typed, never with a council's
    capability. Best effort: a failed write leaves no owner pin, which fails closed."""
    try:
        from core.runtime_paths import data_path

        path = data_path(_OWNER_PIN_FILE)
        pin = _canonical_pin(model)
        if not pin:
            if path.exists():
                path.unlink()
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"model": pin}), encoding="utf-8")
    except Exception:
        pass


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
def owner_key_check(host: str) -> Iterator[None]:
    """Scope of the owner's Verify press in Settings on a key they pasted (``/api/intake/verify``,
    owner-local only). Inside it the door lets through a request that carries a key to ``host``,
    the selected provider's verification host, and nothing else: no keyless request, no other host."""
    token = _OWNER_KEY_CHECK.set(str(host or "").strip().lower().rstrip("."))
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


def _literal_local(url: str) -> bool:
    """Whether a URL names this machine or the LAN by itself: a loopback name, or a loopback,
    private, link-local or CGNAT address. No name is resolved (a lookup is itself a contact)."""
    try:
        import ipaddress

        from core.auto_local_only_mode import _LOOPBACK_NAMES, _address_is_internal

        host = str(urlsplit(str(url or "")).hostname or "").strip().lower().strip("[]")
        if not host:
            return False
        if host in _LOOPBACK_NAMES:
            return True
        try:
            return bool(_address_is_internal(ipaddress.ip_address(host)))
        except ValueError:
            return False
    except Exception:
        return False


def _custom_endpoint_host() -> str:
    """The owner's custom (BYOK) endpoint host."""
    try:
        from core.cloud_providers import custom_base_url

        url = str(custom_base_url() or "").strip()
        return str(urlsplit(url).hostname or "").lower() if url else ""
    except Exception:
        return ""


def is_cloud_provider_request(url: str, provider_id: str = "") -> bool:
    """Whether a request goes to a cloud model provider, by its host: a provider's base URL host,
    openrouter.ai, the UsePod origin, or the owner's custom endpoint.

    A URL that names this machine or the LAN (a loopback name, a loopback or private address) is
    never one, whatever it is configured as: a UsePod origin or custom endpoint on the owner's own
    hardware is local. The declared ``provider_id`` is not used: other services share ids with
    model providers (the calendar adapter declares ``google``)."""
    host = str(urlsplit(str(url or "")).hostname or "").lower().rstrip(".")
    if not host or _literal_local(url):
        return False
    known_hosts = set(_cloud_hosts())
    custom = _custom_endpoint_host()
    if custom:
        known_hosts.add(custom)
    return any(host == known or host.endswith("." + known) for known in known_hosts)


def _host(url: str) -> str:
    return str(urlsplit(str(url or "")).hostname or "").lower().rstrip(".")


_CATALOG_LANE: ContextVar[str] = ContextVar("windows_cloud_catalog_lane", default="")


@contextlib.contextmanager
def catalog_lane(base_url: str) -> Iterator[None]:
    """Scope of one provider lane's cached-catalog lookup: names the endpoint that lane sends to,
    so ``openrouter_catalog.cached_catalog_row`` can tell a lane on this machine from a cloud one."""
    token = _CATALOG_LANE.set(str(base_url or "").strip())
    try:
        yield
    finally:
        _CATALOG_LANE.reset(token)


def lane_is_off_cloud(base_url: str = "") -> bool:
    """A provider lane whose endpoint is named and is not a cloud provider (this machine, the
    LAN, or any host the door does not count as cloud) -- the same host test the dispatch uses.
    With no argument, the lane named by the enclosing ``catalog_lane`` scope. An empty base URL
    is never off-cloud."""
    url = str(base_url or "").strip() or _CATALOG_LANE.get()
    return bool(url) and not is_cloud_provider_request(url)


def door_refusal(url: str, provider_id: str = "", keyed_or_keyless: str = "") -> CloudContactRefused | None:
    """The outbound door's backstop: the refusal to raise for this request, or None."""
    if not active() or not is_cloud_provider_request(url, provider_id):
        return None
    allowed_host = _OWNER_KEY_CHECK.get()
    if allowed_host and str(keyed_or_keyless or "").strip().lower() == "keyed" and _host(url) == allowed_host:
        return None
    if cloud_contact_allowed():
        return None
    return CloudContactRefused(str(url or ""))


__all__ = [
    "CLOUD_OFF_UI_TEXTS",
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
    "owner_pin",
    "owner_turned_cloud_on",
    "record_owner_pin",
    "refuse_cloud_contact",
]
