"""Served DOM proof: the Settings Memory section shows a Remove button on every saved rule, and pressing it
(after the confirm) takes back exactly that rule on the real daemon.

A real daemon, a real chromium on /settings#memory. Cancelling the confirm removes nothing; accepting it
removes the row from the page and the rule from the store, and the other rule stays, still with its button.
"""
from __future__ import annotations

import json
import time

import tests._reader_served_rig as rig
from tests.served_browser import launch_chromium

RULES = [
    ("si-aaaaaaaaaaaa", "From now on, give me distances in kilometres."),
    ("si-bbbbbbbbbbbb", "Always end your reply with a one-line summary."),
]


def _seed(home) -> None:
    data = home / "data"
    data.mkdir(parents=True, exist_ok=True)
    rows = [{"id": rid, "principal": "owner_local", "workspace": "folder:/srv/projects/atlas", "text": text,
             "topic": [], "created_at": time.time(), "session_id": "seed", "status": "active"} for rid, text in RULES]
    (data / "standing_instructions.json").write_text(
        json.dumps({"version": 1, "instructions": rows}), encoding="utf-8")


def _statuses(home) -> dict[str, str]:
    rows = json.loads((home / "data" / "standing_instructions.json").read_text(encoding="utf-8"))["instructions"]
    return {row["id"]: row["status"] for row in rows}


def test_every_saved_rule_has_a_remove_button_that_takes_back_only_that_rule(tmp_path):
    home = tmp_path / "home"
    _seed(home)
    with rig.CapturingProvider() as provider:
        daemon = rig.ServedDaemon(home, provider=provider)
        try:
            daemon.start(timeout=180)
            manager, browser = launch_chromium()
            try:
                page = browser.new_page()
                page.goto(daemon.base_url + "/settings#memory", wait_until="domcontentloaded")
                page.wait_for_selector("[data-vs-rule] .vs-rule-remove", timeout=30_000)
                rows = page.locator("[data-vs-rule]")
                assert rows.count() == len(RULES)
                for rid, text in RULES:
                    row = page.locator(f'[data-vs-rule="{rid}"]')
                    assert text in row.inner_text()
                    assert row.locator("button.vs-rule-remove").count() == 1

                first = page.locator(f'[data-vs-rule="{RULES[0][0]}"] button.vs-rule-remove')
                # Cancelling the confirm removes nothing.
                page.once("dialog", lambda dialog: dialog.dismiss())
                first.click()
                page.wait_for_timeout(500)
                assert page.locator("[data-vs-rule]").count() == len(RULES)
                assert set(_statuses(home).values()) == {"active"}

                # Accepting it removes exactly that rule.
                messages: list[str] = []

                def _accept(dialog) -> None:
                    messages.append(dialog.message)
                    dialog.accept()

                page.once("dialog", _accept)
                first.click()
                page.wait_for_selector(f'[data-vs-rule="{RULES[0][0]}"]', state="detached", timeout=15_000)
                assert messages and "rule" in messages[0].lower()
                assert _statuses(home) == {RULES[0][0]: "taken_back", RULES[1][0]: "active"}
                remaining = page.locator("[data-vs-rule]")
                assert remaining.count() == 1
                assert remaining.locator("button.vs-rule-remove").count() == 1
            finally:
                browser.close()
                manager.stop()
        finally:
            daemon.stop()
