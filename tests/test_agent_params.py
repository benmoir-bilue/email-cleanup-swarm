"""Per-model request shapes.

Each tier rejects a different parameter, and getting it wrong is an HTTP 400 rather
than a degraded answer — so the rules are pinned here rather than discovered in
production halfway through a paid batch.
"""

from __future__ import annotations

import pytest

from ecs import config
from ecs.agents.client import CostTracker, build_params

BASE = {
    "system": "system prompt",
    "messages": [{"role": "user", "content": "hello"}],
    "max_tokens": 4000,
}


class TestHaiku:
    """Haiku 4.5 rejects `output_config.effort` outright."""

    def test_effort_is_never_sent(self):
        params = build_params(config.MODEL_TRIAGE, effort="high", **BASE)
        assert "effort" not in params.get("output_config", {})

    def test_thinking_uses_the_legacy_budget_form(self):
        params = build_params(config.MODEL_TRIAGE, thinking=True, **BASE)
        assert params["thinking"]["type"] == "enabled"
        # A budget >= max_tokens is rejected by the API.
        assert params["thinking"]["budget_tokens"] < BASE["max_tokens"]
        assert params["thinking"]["budget_tokens"] >= 1024

    def test_no_thinking_key_when_not_requested(self):
        params = build_params(config.MODEL_TRIAGE, **BASE)
        assert "thinking" not in params


class TestOpus:
    """Opus 5 thinks by default; disabling is only legal at effort <= high."""

    def test_adaptive_thinking_with_summary_when_requested(self):
        params = build_params(
            config.MODEL_STRATEGIST, effort="high", thinking=True, **BASE
        )
        assert params["thinking"] == {"type": "adaptive", "display": "summarized"}
        assert params["output_config"]["effort"] == "high"

    @pytest.mark.parametrize("effort", [None, "low", "medium", "high"])
    def test_thinking_can_be_disabled_at_or_below_high(self, effort):
        params = build_params(config.MODEL_STRATEGIST, effort=effort, **BASE)
        assert params["thinking"] == {"type": "disabled"}

    @pytest.mark.parametrize("effort", ["xhigh", "max"])
    def test_thinking_is_not_disabled_above_high(self, effort):
        """Pairing disabled thinking with xhigh/max returns 400, so we must not."""
        params = build_params(config.MODEL_STRATEGIST, effort=effort, **BASE)
        assert "thinking" not in params


class TestFable:
    """Fable 5 rejects ANY explicit thinking config — the key must be absent."""

    @pytest.mark.parametrize("thinking", [True, False])
    def test_thinking_key_is_always_omitted(self, thinking):
        params = build_params(
            config.MODEL_CHALLENGER, effort="high", thinking=thinking, **BASE
        )
        assert "thinking" not in params

    def test_effort_is_the_only_depth_control(self):
        params = build_params(config.MODEL_CHALLENGER, effort="max", **BASE)
        assert params["output_config"]["effort"] == "max"


class TestStructuredOutput:
    def test_schema_becomes_output_config_format(self):
        schema = {"type": "object", "properties": {}, "additionalProperties": False}
        params = build_params(config.MODEL_TRIAGE, schema=schema, **BASE)
        assert params["output_config"]["format"] == {
            "type": "json_schema",
            "schema": schema,
        }

    def test_schema_and_effort_coexist_under_output_config(self):
        schema = {"type": "object"}
        params = build_params(
            config.MODEL_STRATEGIST, schema=schema, effort="high", **BASE
        )
        assert params["output_config"]["effort"] == "high"
        assert params["output_config"]["format"]["schema"] == schema

    def test_no_output_config_when_neither_is_set(self):
        params = build_params(config.MODEL_TRIAGE, **BASE)
        assert "output_config" not in params


class TestCostTracker:
    class FakeUsage:
        def __init__(self, i=0, o=0, cr=0, cw=0):
            self.input_tokens = i
            self.output_tokens = o
            self.cache_read_input_tokens = cr
            self.cache_creation_input_tokens = cw

    def test_haiku_pricing(self):
        t = CostTracker()
        t.add("claude-haiku-4-5", self.FakeUsage(i=1_000_000, o=1_000_000))
        # $1 in + $5 out per MTok
        assert t.total_cost == pytest.approx(6.0)

    def test_batch_is_half_price(self):
        t = CostTracker()
        t.add("claude-haiku-4-5", self.FakeUsage(i=1_000_000), batch=True)
        assert t.total_cost == pytest.approx(0.5)

    def test_cache_reads_are_a_tenth_of_input(self):
        t = CostTracker()
        t.add("claude-opus-5", self.FakeUsage(cr=1_000_000))
        # $5/MTok input, cache read ~0.1x
        assert t.total_cost == pytest.approx(0.5)

    def test_fable_is_the_expensive_tier(self):
        t = CostTracker()
        t.add("claude-fable-5", self.FakeUsage(i=1_000_000, o=100_000))
        assert t.total_cost == pytest.approx(10.0 + 5.0)

    def test_unknown_model_does_not_crash(self):
        t = CostTracker()
        t.add("some-future-model", self.FakeUsage(i=1000, o=1000))
        assert t.total_cost == 0.0

    def test_summary_mentions_every_model_used(self):
        t = CostTracker()
        t.add("claude-haiku-4-5", self.FakeUsage(i=100))
        t.add("claude-opus-5", self.FakeUsage(i=100))
        summary = t.summary()
        assert "claude-haiku-4-5" in summary
        assert "claude-opus-5" in summary
        assert "total" in summary
