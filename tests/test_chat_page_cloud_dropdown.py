"""The live OpenRouter model dropdown: free-first, orderable, price-optional, and every switch
goes through the tested /api/cloud/model endpoint — with the static popover untouched and a
fail-soft path when the catalog cannot be fetched."""
from __future__ import annotations

import json

import pytest

from core.vool_chat_page import render_vool_chat_html
from tests.chat_page_js_harness import DOM, run_node, script

HTML = render_vool_chat_html()


def _switch_probe(*, code: str, accept: bool = False) -> dict:
    """Drive the real page's switch path; no browser or provider connection."""
    return run_node(DOM + "\n" + script() + "\n" + """
    (async () => {
      const code = CODE;
      const posts = [], notices = [], reviews = [];
      const before = modelForChat(displayedChat);
      toast = (message) => notices.push(message);
      window.VoolPriceGate = { review: async (request) => {
        reviews.push(request); return ACCEPT;
      }};
      modelSelectionPost = async (body) => {
        posts.push(body);
        if (body.confirm_paid === true) {
          return { r: { ok: true, status: 200 },
                   j: { ok: true, model: body.model, provider: 'openrouter' } };
        }
        return { r: { ok: false, status: code === 'failed' ? 500 : 409 },
                 j: code === 'failed' ? {} : { code } };
      };
      const switched = await switchCloudModel('vendor/test', 'Test', null, 'openrouter');
      out({ switched, before,
        after: modelForChat(displayedChat), posts, notices, reviews });
      process.exit(0);
    })().catch((error) => { console.error(error); process.exit(1); });
    """.replace("CODE", json.dumps(code)).replace("ACCEPT", json.dumps(accept)))


def test_dropdown_pulls_the_live_catalog():
    assert "function renderCloudModels" in HTML
    assert "/api/cloud/models?order=" in HTML


def test_free_models_are_pinned_above_paid():
    # Group labels are now provider-aware ("FREE · <provider label>"); free renders before paid.
    assert "FREE · ' + provLabel" in HTML
    assert "PAID · ' + provLabel" in HTML
    assert HTML.index("(m) => m.free)") < HTML.index("(m) => !m.free)")


def test_ordering_is_user_selectable_and_persisted():
    for order in ("featured", "name", "context"):
        assert order in HTML, order
    assert "vool_model_order" in HTML


def test_prices_are_optional_and_persisted():
    assert "vool_model_prices" in HTML
    assert "per 1M" in HTML
    assert "function priceText" in HTML


def test_switch_goes_through_the_guarded_endpoint():
    assert "function switchCloudModel" in HTML
    assert "'/api/cloud/model'" in HTML
    # only a real ok response updates the selection
    assert "if (r.ok && j.ok)" in HTML
    result = _switch_probe(code="failed")
    assert result["errors"] == []
    assert result["switched"] is False
    assert result["after"] == result["before"]
    assert result["notices"] == ["Could not select this model. Try again."]
    assert len(result["posts"]) == 1
    assert "confirm_paid" not in result["posts"][0]


def test_static_popover_is_untouched_and_failsoft():
    # Auto is the only static model row; exact free and paid pins come from the live catalog.
    assert 'data-model="vool"' in HTML
    assert 'data-model="openrouter-auto"' not in HTML
    assert 'data-model="openrouter-frontier"' not in HTML
    # dynamic rows are a separate class so the static click handler skips them
    assert ":not(.cloud-dyn)" in HTML
    assert "function clearCloudModels" in HTML
    # paid list is capped
    assert ".slice(0, 30)" in HTML


def test_persisted_cloud_id_survives_reload():
    # a well-formed vendor/name[:tag] value is kept, not reset to 'vool'
    assert "CLOUD_MODEL_ID_RE.test(modelValue)" in HTML


def test_removing_the_key_reverts_a_cloud_model_selection():
    # When the key goes away, a previously-selected cloud model must revert to local Auto so a
    # send never targets an unusable model while the pill reads 'Local only'.
    assert "modelValue = 'vool'" in HTML
    assert "modelValue.indexOf('openrouter') === 0" in HTML


def test_no_banned_language_in_the_touched_region():
    # Moonshot is a supported provider, not prohibited editorial copy. Its identifier
    # also belongs in routing expressions, so a whole-page word ban is invalid.
    assert "moonshot: 'Moonshot'" in HTML
    for banned in ("stay honest", "honestly", "brutal", "goblin"):
        assert banned not in HTML, banned


def test_auto_fallback_is_separate_from_explicit_model_pins():
    assert "Auto fallback" in HTML
    assert "/api/cloud/auto-model" in HTML
    assert "Best verified free" in HTML
    assert "used only by VOOL Auto; never paid" in HTML
    assert "free · exact pin" in HTML
    assert "paid · exact pin" in HTML


@pytest.mark.parametrize("code", [
    "paid_model_confirm_required", "MODEL_COST_UNKNOWN", "PAID_STATUS_UNKNOWN", "price_above_accepted",
])
@pytest.mark.parametrize("accept", [False, True])
def test_paid_pin_is_explicit_and_project_memory_does_not_silently_reset_it(code, accept):
    result = _switch_probe(code=code, accept=accept)
    assert result["errors"] == []
    assert result["switched"] is accept
    assert len(result["reviews"]) == 1
    assert result["reviews"][0]["code"] == code
    assert "confirm_paid" not in result["posts"][0]
    if accept:
        assert len(result["posts"]) == 2
        assert result["posts"][1]["confirm_paid"] is True
        assert result["after"] == "vendor/test"
    else:
        assert len(result["posts"]) == 1
        assert result["after"] == result["before"]
    assert "setModelValue(remembered)" in HTML
    assert "switched to local free" not in HTML
