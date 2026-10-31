"""Pricing-table guards: a model missing from _PRICING silently bills at Sonnet 4.6 rates.

Rates verified against Anthropic's pricing page on 2026-10-30. Costs are asserted in
dollars per 1M tokens so a typo'd per-1K figure in _PRICING is caught.
"""
import pytest

from spendgaugeai.database import _PRICING, _estimate_cost, _lookup_pricing

M = 1_000_000


@pytest.mark.parametrize("model,inp,out,cache_read,write_5m", [
    ("claude-haiku-5-5", 0.10, 0.50, 0.01, 0.125),
    ("claude-sonnet-5-5", 2.00, 10.00, 0.10, 2.50),
    ("claude-opus-5-5", 4.00, 20.00, 0.20, 5.00),
    ("claude-sonnet-5", 2.00, 10.00, 0.20, 2.50),
    ("claude-haiku-4-5", 1.00, 5.00, 0.10, 1.25),
    ("claude-opus-4-8", 5.00, 25.00, 0.50, 6.25),
])
def test_rates_per_million(model, inp, out, cache_read, write_5m):
    assert _estimate_cost(model, M, 0, 0, 0) == pytest.approx(inp)
    assert _estimate_cost(model, 0, 0, 0, M) == pytest.approx(out)
    assert _estimate_cost(model, 0, 0, M, 0) == pytest.approx(cache_read)
    assert _estimate_cost(model, 0, M, 0, 0) == pytest.approx(write_5m)
    # 1-hour cache writes bill at 2x input.
    assert _estimate_cost(model, 0, M, 0, 0, cache_write_1h=M) == pytest.approx(2 * inp)


@pytest.mark.parametrize("model", ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"])
def test_new_models_are_not_priced_by_fallback(model):
    assert model in _PRICING
    # A dated response.model suffix must still resolve to the new entry, not to a shorter prefix.
    # Compare by value, not identity: other test modules reload spendgaugeai.database,
    # which rebinds its _PRICING to a new (equal) dict object.
    assert _lookup_pricing(f"{model}-20261001") == _PRICING[model]


def test_haiku_5_5_does_not_resolve_to_haiku_4_5():
    assert _lookup_pricing("claude-haiku-5-5") != _PRICING["claude-haiku-4-5"]
