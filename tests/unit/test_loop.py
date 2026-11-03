"""Tests for /loop N <question>: the helper in main.py, with chat() mocked."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy-test-key")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import llm_light.state as state
from llm_light import main


class TestLoopCommand(unittest.TestCase):

    def setUp(self):
        self._saved = (state.session_budget, state.session_cost)
        state.session_budget = None
        state.session_cost = 0.0
        self.chat = patch("llm_light.main.chat").start()
        self.save = patch("llm_light.session.save").start()
        patch("llm_light.main._index_ready_for_query", return_value=False).start()
        self.addCleanup(patch.stopall)

    def tearDown(self):
        state.session_budget, state.session_cost = self._saved

    def test_runs_question_n_times(self):
        main._loop_command("3 what does OrderService do?", auto_apply=False)
        self.assertEqual(self.chat.call_count, 3)
        for call in self.chat.call_args_list:
            self.assertEqual(call.args[0], "what does OrderService do?")
            self.assertFalse(call.kwargs["auto_apply"])
        self.assertEqual(self.save.call_count, 3)

    def test_bad_counts_rejected(self):
        for bad in ("0", "21", "-1", "abc", ""):
            self.chat.reset_mock()
            main._loop_command(f"{bad} hello", auto_apply=False)
            self.chat.assert_not_called()

    def test_missing_question_rejected(self):
        main._loop_command("3", auto_apply=False)
        self.chat.assert_not_called()

    def test_stops_on_keyboard_interrupt(self):
        self.chat.side_effect = [None, KeyboardInterrupt()]
        main._loop_command("5 hello", auto_apply=False)
        self.assertEqual(self.chat.call_count, 2)

    def test_stops_on_exception(self):
        self.chat.side_effect = [None, RuntimeError("boom")]
        main._loop_command("5 hello", auto_apply=False)
        self.assertEqual(self.chat.call_count, 2)

    def test_stops_when_budget_reached(self):
        state.session_budget = 1.0

        def spend(*args, **kwargs):
            state.session_cost += 0.6

        self.chat.side_effect = spend
        main._loop_command("5 hello", auto_apply=False)
        # After turn 1 the cost is 0.6 (under 1.0), after turn 2 it is 1.2: stop before turn 3.
        self.assertEqual(self.chat.call_count, 2)


if __name__ == "__main__":
    unittest.main()
