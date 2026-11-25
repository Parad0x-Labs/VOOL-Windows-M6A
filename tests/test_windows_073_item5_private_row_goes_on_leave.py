"""Served proof (0.7.3 carry item 5): the page-only "Private" sidebar row goes when you leave that chat.

A chat that starts private has no server record, so its sidebar row lives in the chat page's memory
only. It used to stay there until VOOL restarted, after the person had moved to another chat or started
a new one. Real Chromium on the shipped chat page against one isolated daemon: the row is shown while
the private chat is on screen, and is gone once the person opens another chat or starts a new one. An
ordinary chat's row stays. The chat page is the same on every host, so this holds on all hosts.
Authored wording only.
"""
from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

SECRET = "Quillmere"  # authored marker for private words

SIDEBAR = """(() => [...document.querySelectorAll('#sessions .session')].map(s => ({
  title: (s.querySelector('.s-title') || {}).textContent || '',
  private: s.classList.contains('session-private'),
})))()"""


def test_the_private_row_goes_when_the_person_leaves_the_private_chat(tmp_path):
    import tests._reader_served_rig as rig
    from tests.served_browser import launch_chromium

    def reply(body):
        from core.entity_ambiguity import AMBIGUITY_SYSTEM_PROMPT

        if any(AMBIGUITY_SYSTEM_PROMPT in str(m.get("content", "")) for m in body.get("messages") or []):
            return json.dumps({"ambiguous": False, "referents": [], "clarification": ""})
        return "Sounds lovely."

    def answers(page) -> int:
        return page.evaluate("() => document.querySelectorAll('#log .msg.assistant:not(.pending)').length")

    def send(page, text: str) -> None:
        before = answers(page)
        page.fill("#input", text)
        page.click("#send")
        page.wait_for_function(
            "n => document.querySelectorAll('#log .msg.assistant:not(.pending)').length > n "
            "&& !document.querySelector('#send').textContent.includes('Queue')",
            arg=before, timeout=180_000)
        page.wait_for_timeout(400)

    def sidebar(page, refresh: bool = True) -> list[dict[str, Any]]:
        if refresh:
            page.evaluate("() => loadSessions()")
            page.wait_for_timeout(300)
        return page.evaluate(SIDEBAR)

    def start_private(page) -> None:
        page.click("#newChat")
        page.wait_for_timeout(200)
        send(page, "don't record this")
        page.wait_for_selector("#privateBanner:not([hidden])", timeout=10_000)
        send(page, f"The surprise party is at {SECRET} Hall.")

    regular = {"title": "My cat is called Biscuit.", "private": False}
    private = {"title": "Private", "private": True}

    with rig.CapturingProvider(reply_fn=reply) as provider:
        daemon = rig.ServedDaemon(tmp_path / "home", provider=provider,
                                  env_extra={"VOOL_RAW_OLLAMA_API_URL": provider.base_url})
        daemon.register_provider()
        daemon.start()
        try:
            subprocess.run([sys.executable, "-c",
                "from storage.model_provider_manifest import list_provider_manifests; "
                "from tests._authorship_certification import certify_for_authorship; "
                "[certify_for_authorship(m) for m in list_provider_manifests() "
                "if m.provider_name == 'reader-stub' and m.model_name == 'reader-drive:stub']"
            ], cwd=rig.REPO_ROOT, env=daemon.env(), check=True, capture_output=True, timeout=30)
            manager, browser = launch_chromium()
            try:
                page = browser.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.route("**/api/tags", lambda route: route.fulfill(
                    content_type="application/json", body=json.dumps({"models": [{"name": daemon.model}]})))
                page.goto(daemon.base_url + "/chat", wait_until="domcontentloaded")
                page.wait_for_selector("#input", timeout=30_000)
                page.click("#modelBtn")
                page.locator(f'.local-dyn[data-model="{daemon.model}"]').click()

                # An ordinary chat first: it gets its own row from the server.
                send(page, regular["title"])
                assert sidebar(page) == [regular]

                # A new chat that starts private: its page-only row shows while it is on screen.
                start_private(page)
                rows = sidebar(page)
                assert sorted(rows, key=lambda r: r["title"]) == [regular, private], rows

                # Leave it by opening the ordinary chat: the Private row goes at once, and stays gone
                # after the sidebar is read from the server again. The ordinary chat's row stays.
                page.locator("#sessions .session", has_text=regular["title"]).locator(".s-title").click()
                page.wait_for_function(
                    "() => !document.querySelector('#sessions .session.session-private')", timeout=10_000)
                assert sidebar(page, refresh=False) == [regular]
                assert sidebar(page) == [regular]

                # Leave a private chat by starting a new one: the Private row goes too.
                start_private(page)
                assert sorted(sidebar(page), key=lambda r: r["title"]) == [regular, private]
                page.click("#newChat")
                page.wait_for_function(
                    "() => !document.querySelector('#sessions .session.session-private')", timeout=10_000)
                assert sidebar(page) == [regular]
                assert SECRET not in page.evaluate("() => document.getElementById('sessions').textContent")
                assert errors == [], errors
            finally:
                browser.close()
                manager.stop()
        finally:
            daemon.stop()
