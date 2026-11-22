"""Tests for /budget: the command parser and the router's over-budget downgrade."""

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy-test-key")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import llm_light.state as state
from llm_light import llm
from llm_light.main import _budget_command


class TestBudget(unittest.TestCase):

    def setUp(self):
        self._saved = (state.session_budget, state.session_cost, state.budget_asked)
        state.session_budget = None
        state.session_cost = 0.0
        state.budget_asked = True

    def tearDown(self):
        state.session_budget, state.session_cost, state.budget_asked = self._saved

    def test_set_show_and_clear(self):
        self.assertIn("No budget", _budget_command(""))
        self.assertIn("$2.50", _budget_command("2.5"))
        self.assertEqual(state.session_budget, 2.5)
        self.assertFalse(state.budget_asked)
        self.assertIn("$2.50", _budget_command(""))
        _budget_command("off")
        self.assertIsNone(state.session_budget)

    def test_dollar_sign_accepted(self):
        _budget_command("$1")
        self.assertEqual(state.session_budget, 1.0)

    def test_bad_values_rejected(self):
        for bad in ("abc", "-3", "nan", "inf"):
            self.assertIn("Usage", _budget_command(bad), bad)
            self.assertIsNone(state.session_budget)

    def test_under_budget_keeps_route(self):
        state.session_budget = 5.0
        state.session_cost = 1.0
        # A Sonnet route: an Opus route goes through the top-tier prompt (test_top_tier.py).
        model, effort, _ = llm._finalize_route(llm.MODEL_SONNET, "high", 8_192)
        self.assertEqual((model, effort), (llm.MODEL_SONNET, "high"))

    def test_over_budget_drops_to_cheapest(self):
        state.session_budget = 1.0
        state.session_cost = 1.0
        model, effort, max_tokens = llm._finalize_route(llm.MODEL_OPUS, "max", 16_000)
        self.assertEqual((model, effort, max_tokens), (llm.MODEL_HAIKU, "low", 2_048))

    def test_no_budget_never_downgrades(self):
        state.session_cost = 1000.0
        model, _, _ = llm._finalize_route(llm.MODEL_SONNET, "high", 8_192)
        self.assertEqual(model, llm.MODEL_SONNET)


class TestEstimateWithWebSearch(unittest.TestCase):
    """With --web the model may search, at $0.01 each and at most 5 per request; the
    route line's estimate shows that as a range instead of tokens only (#245)."""

    def setUp(self):
        self._saved = (state.session_budget, state.session_cost, state.budget_asked)
        state.session_budget, state.session_cost, state.budget_asked = 5.0, 0.0, True

    def tearDown(self):
        state.session_budget, state.session_cost, state.budget_asked = self._saved

    def _route_line(self, web):
        import io
        import re
        from unittest.mock import patch
        import llm_light.config as config
        err = io.StringIO()
        with patch.object(config, "WEB_ACCESS", web), patch("sys.stderr", err):
            llm._finalize_route(llm.MODEL_SONNET, "high", 8_192)
        return [float(x) for x in re.findall(r"\$(\d+\.\d+)", err.getvalue())]

    def test_web_access_shows_the_search_ceiling(self):
        low, high = self._route_line(web=True)
        self.assertAlmostEqual(high - low, 0.05, places=3)

    def test_without_web_one_figure(self):
        self.assertEqual(len(self._route_line(web=False)), 1)


if __name__ == "__main__":
    unittest.main()
