"""SERVED: on Windows the chat card for a transfer request carries its own Reject (Steve finding on 0.7.3, fixed in 0.7.5).

Before this, turning a request down meant opening "Review transfer" and finding "Cancel request" in the sheet. The
wallet status now says ``card_reject`` (true on Windows only) and the one card script draws a Reject beside Review.
Reject uses the same reject door as the sheet's Cancel and ends the request with nothing signed or sent; it never
quotes or approves. Approval still goes only through the review sheet and the PIN. Off Windows the card is unchanged.

SCRIPTED MODEL, SIMULATED CHAIN (Solana Devnet node), real daemon, the confined browser. This Linux daemon reports
``card_reject: false``, so the Windows answer is the daemon's own status with that one field set, as a Windows
daemon sends it.
"""
from __future__ import annotations

import json
import sys
import uuid
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests import served_browser
from tests.wallet._rig import DEVNET_GENESIS, ScriptedRpc
from tests.wallet._rig_provider import MODEL, PromptRoutedProvider, seed_daemon

pytestmark = [pytest.mark.safety, pytest.mark.served]

PIN = "482913"
SOLANA_DEVNET = "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"


def _door(daemon, path: str, body: dict) -> tuple[int, dict]:
    request = Request(daemon.base_url + path, data=json.dumps(body).encode(), method="POST", headers={"Origin": daemon.base_url, "Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=120) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _sol_key() -> str:
    from core.vool_wallet import b58encode

    return b58encode(Ed25519PrivateKey.generate().public_key().public_bytes_raw())


@pytest.fixture(scope="module")
def browser():
    ctx, browser = served_browser.launch_chromium()
    try:
        yield browser
    finally:
        browser.close()
        ctx.stop()


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    from tests._blackbox_served_rig import ServedDaemon

    home = tmp_path_factory.mktemp("cardreject") / "home"
    sol = ScriptedRpc(genesis_hash=DEVNET_GENESIS)
    sol.real_signature = True
    sol.status_keyed = True
    provider = PromptRoutedProvider()
    for node in (sol, provider):
        node.__enter__()
    daemon = ServedDaemon(home, env_extra={
        "VOOL_ALWAYS_ON_CATALOG": "1", "VOOL_WALLET_ENABLED": "1", "VOOL_WALLET_RPC_URLS": json.dumps({SOLANA_DEVNET: sol.url}),
        "VOOL_WALLET_X402_ALLOW_LOOPBACK": "1", "OLLAMA_HOST": provider.base_url, "VOOL_OLLAMA_URL": provider.base_url,
        "VOOL_OLLAMA_CHAT_URL": f"{provider.base_url}/api/chat", "VOOL_OLLAMA_PS_URL": f"{provider.base_url}/api/ps", "VOOL_OLLAMA_TAGS_URL": f"{provider.base_url}/api/tags",
    })
    try:
        try:
            daemon.start(timeout=240)
        except Exception as exc:  # pragma: no cover - environment
            pytest.skip(f"served daemon could not boot here: {exc}")
        seed_daemon(home, provider.base_url)
        provider.reset()
        status, switched = _door(daemon, "/api/wallet/environment", {"environment": "testnet"})
        assert status == 200, switched
        status, created = _door(daemon, "/api/wallet/setup/create", {"network": SOLANA_DEVNET, "method": "pin", "credential": PIN, "credential_confirmation": PIN, "creation_key": f"reject-{uuid.uuid4().hex}", "label": "card reject"})
        assert status == 200, created
        wallet_id = created["setup"]["wallet_id"]
        status, revealed = _door(daemon, "/api/wallet/setup/reveal", {"wallet_id": wallet_id, "credential": PIN})
        token = revealed["backup"]["ack_token"]
        revealed = None
        status, ready = _door(daemon, "/api/wallet/setup/acknowledge", {"wallet_id": wallet_id, "ack_token": token})
        assert status == 200 and ready["setup"]["setup_state"] == "ready", ready
        yield daemon, sol, provider
    finally:
        daemon.stop()
        for node in (provider, sol):
            node.__exit__(None, None, None)


def _chat_proposal(daemon, message: str, *, session: str) -> str:
    turn = daemon.chat(message, session_id=session, model=MODEL, mode="auto")
    text = str((turn.get("message") or {}).get("content") or "")
    assert "pending_approval" in text and "pay-" in text, text
    return next(word.strip(".,:") for word in text.split() if word.startswith("pay-"))


def _open_chat(daemon, browser, *, windows: bool):
    page = browser.new_page()
    errors: list[str] = []
    posts: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("request", lambda request: posts.append(request.url.rsplit("/", 1)[-1]) if request.method == "POST" and "/api/wallet/" in request.url else None)
    page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(daemon.base_url) else route.abort())

    def as_windows(route):
        response = route.fetch()
        body = response.json()
        assert body["status"]["card_reject"] is (sys.platform == "win32")
        body["status"]["card_reject"] = True
        route.fulfill(response=response, body=json.dumps(body))

    if windows:
        page.route(daemon.base_url + "/api/wallet/status", as_windows)
    page.goto(daemon.base_url + "/chat", wait_until="domcontentloaded")
    return page, errors, posts


def test_a_windows_card_rejects_without_the_review_and_never_approves(served, browser):
    daemon, node, provider = served
    pid = _chat_proposal(daemon, f"Send 0.0004 SOL on solana to {_sol_key()} for reject one", session="reject-1")
    page, errors, posts = _open_chat(daemon, browser, windows=True)
    try:
        card = page.locator(f'.vw-card[data-proposal="{pid}"]')
        card.locator(".vw-card-reject").wait_for(timeout=30_000)
        # Review is still there, and nothing on the card asks for a PIN or approves
        assert card.locator(".vw-review").count() == 1
        assert card.locator("input").count() == 0 and card.locator(".vw-approve").count() == 0
        assert card.locator(".vw-card-reject").text_content() == "Reject"
        sends = node.send_count()
        with page.expect_response(lambda r: r.url == daemon.base_url + "/api/wallet/reject"):
            card.locator(".vw-card-reject").click()
        page.wait_for_function(f"document.querySelector('.vw-card[data-proposal=\"{pid}\"] .vw-result').textContent.length > 0", timeout=15_000)
        assert card.locator(".vw-result").text_content() == "Cancelled. Nothing was signed or sent."
        assert card.locator(".vw-review").count() == 0 and card.locator(".vw-card-reject").count() == 0
        assert page.locator("#vwSheet").count() == 0
        with urlopen(daemon.base_url + f"/api/wallet/proposals/{pid}", timeout=30) as response:
            assert json.load(response)["proposal"]["state"] == "rejected"
        assert node.send_count() == sends
        assert posts.count("reject") == 1 and "approve" not in posts and "quote" not in posts
        assert not errors, errors
    finally:
        page.unroute_all(behavior="ignoreErrors")
        page.close()


def test_a_windows_card_still_approves_only_through_the_review_and_pin(served, browser):
    daemon, node, _provider = served
    pid = _chat_proposal(daemon, f"Send 0.0005 SOL on solana to {_sol_key()} for reject two", session="reject-2")
    page, errors, posts = _open_chat(daemon, browser, windows=True)
    try:
        card = page.locator(f'.vw-card[data-proposal="{pid}"]')
        card.locator(".vw-card-reject").wait_for(timeout=30_000)
        with page.expect_response(lambda r: r.url == daemon.base_url + "/api/wallet/quote"):
            card.locator(".vw-review").click()
        page.wait_for_function("!document.getElementById('vwSheetApprove').disabled", timeout=15_000)
        # with the review open the modal covers the card; even a scripted click on the card's Reject hands the
        # decision back to the sheet instead of acting
        card.locator(".vw-card-reject").evaluate("button => button.click()")
        page.wait_for_timeout(500)
        assert "reject" not in posts
        sends = node.send_count()
        # a wrong PIN approves nothing
        page.locator("#vwSheetCredential").fill("000000" if PIN != "000000" else "111111")
        with page.expect_response(lambda r: r.url == daemon.base_url + "/api/wallet/approve") as wrong:
            page.locator("#vwSheetApprove").click()
        assert wrong.value.status != 200
        assert node.send_count() == sends
        page.wait_for_function("!document.getElementById('vwSheetApprove').disabled", timeout=15_000)
        page.locator("#vwSheetCredential").fill(PIN)
        with page.expect_response(lambda r: r.url == daemon.base_url + "/api/wallet/approve"):
            page.locator("#vwSheetApprove").click()
        page.wait_for_function(f"document.querySelector('.vw-card[data-proposal=\"{pid}\"] .vw-transfer-label') !== null", timeout=30_000)
        assert node.send_count() == sends + 1
        assert posts.count("approve") == 2 and "reject" not in posts
        assert card.locator(".vw-card-reject").count() == 0
        assert not errors, errors
    finally:
        page.unroute_all(behavior="ignoreErrors")
        page.close()


def test_off_windows_the_card_is_unchanged(served, browser):
    daemon, _node, _provider = served
    pid = _chat_proposal(daemon, f"Send 0.0006 SOL on solana to {_sol_key()} for reject three", session="reject-3")
    with urlopen(daemon.base_url + "/api/wallet/status", timeout=30) as response:
        assert json.load(response)["status"]["card_reject"] is (sys.platform == "win32")
    if sys.platform == "win32":
        pytest.skip("this daemon is a Windows host")
    page, errors, _posts = _open_chat(daemon, browser, windows=False)
    try:
        card = page.locator(f'.vw-card[data-proposal="{pid}"]')
        card.locator(".vw-review").wait_for(timeout=30_000)
        page.wait_for_timeout(500)
        assert card.locator(".vw-card-reject").count() == 0
        assert not errors, errors
    finally:
        page.unroute_all(behavior="ignoreErrors")
        page.close()
