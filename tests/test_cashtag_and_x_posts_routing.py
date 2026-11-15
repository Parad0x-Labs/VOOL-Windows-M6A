"""0.7.2 claims rows 2 and 3, as Steve found them on 14 Nov 2026.

Row 3: "collect the last 7 days of trades for $BONK on Solana" was stored and routed as "$ BONK", because the input
normalizer split the cashtag, so market.collect never seated. Row 2: "read my latest posts on X" went to web search,
because the X posts rule only read the "my X posts" order. Both rows read the text the way a turn does: normalized
first, then the demand signals.
"""

from __future__ import annotations

import pytest

from core.input_normalizer import normalize_user_text
from core.tool_demand_signals import resolve_demand_signals


def _intents(text: str) -> tuple[str, ...]:
    return resolve_demand_signals(normalize_user_text(text).normalized_text).explicit_intents


@pytest.mark.parametrize(
    "typed",
    [
        "Collect the last 7 days of trades for $BONK on Solana",
        "price of $wif?",
        "what is $BTC.",
        "watch $PEPE and $WIF",
    ],
)
def test_a_cashtag_keeps_its_dollar_sign_through_normalizing(typed: str) -> None:
    normalized = normalize_user_text(typed).normalized_text
    for word in typed.replace("?", " ").replace(".", " ").split():
        if word.startswith("$"):
            assert word in normalized, (typed, normalized)


@pytest.mark.parametrize("typed", ["send $50 to bob", "I paid US$5"])
def test_money_keeps_its_old_spacing(typed: str) -> None:
    # A cashtag starts with a letter, so an amount takes the old path unchanged.
    assert "$ " in normalize_user_text(typed).normalized_text


def test_the_row_3_request_seats_market_collect_after_normalizing() -> None:
    assert "market.collect" in _intents("Collect the last 7 days of trades for $BONK on Solana")


@pytest.mark.parametrize(
    "typed",
    ["read my latest posts on X", "show my 5 posts on Twitter", "read my recent tweets from X"],
)
def test_my_posts_on_x_seats_the_x_posts_read(typed: str) -> None:
    assert "x.user_posts" in _intents(typed)


@pytest.mark.parametrize("typed", ["I wrote my posts on X.", "fix my posts on xyz site", "my posts are on the blog"])
def test_other_posts_sentences_do_not_seat_x(typed: str) -> None:
    assert "x.user_posts" not in _intents(typed)
