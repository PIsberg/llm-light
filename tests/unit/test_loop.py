"""Tests for /loop: the parsing and stop rules in main.py.

chat() swallows Ctrl+C and API errors and prints them, so a test that mocks chat
to raise proves nothing about a real turn. TestLoopThroughRealChat keeps the real
chat() and fakes only the provider's complete().
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy-test-key")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import llm_light.state as state
from llm_light import commands, main
from llm_light.providers.base import NotLoggedIn
from llm_light.providers.claude import ClaudeProvider


class _LoopCase(unittest.TestCase):

    def setUp(self):
        self._saved = (state.session_budget, state.session_cost)
        state.session_budget = None
        state.session_cost = 0.0
        self.save = patch("llm_light.session.save").start()
        self.sleep = patch("llm_light.main.time.sleep").start()
        patch("llm_light.main._index_ready_for_query", return_value=False).start()
        patch("builtins.print").start()
        self.addCleanup(patch.stopall)

    def tearDown(self):
        state.session_budget, state.session_cost = self._saved
        state.pending_context.clear()


class TestLoopCommand(_LoopCase):

    def setUp(self):
        super().setUp()
        self.chat = patch("llm_light.main.chat", return_value=True).start()

    def test_runs_question_n_times(self):
        main._loop_command("3 what does OrderService do?", auto_apply=False)
        self.assertEqual(self.chat.call_count, 3)
        for call in self.chat.call_args_list:
            self.assertEqual(call.args[0], "what does OrderService do?")
            self.assertFalse(call.kwargs["auto_apply"])
        self.assertEqual(self.save.call_count, 3)
        self.sleep.assert_not_called()  # a count does not wait between turns

    def test_bad_counts_and_intervals_rejected(self):
        for bad in ("0", "21", "-1", "abc", "", "0m", "5x", "1_0"):
            self.chat.reset_mock()
            main._loop_command(f"{bad} hello", auto_apply=False)
            self.chat.assert_not_called()

    def test_missing_question_rejected(self):
        for arg in ("3", "5m"):
            main._loop_command(arg, auto_apply=False)
            self.chat.assert_not_called()

    def test_interval_waits_between_turns_and_is_capped(self):
        main._loop_command("5m is the build green?", auto_apply=False)
        self.assertEqual(self.chat.call_count, main.LOOP_MAX)
        # Between turns only: nothing to wait for after the last one.
        self.assertEqual(self.sleep.call_count, main.LOOP_MAX - 1)
        self.sleep.assert_called_with(300)

    def test_interval_units(self):
        for text, seconds in (("30s", 30), ("2m", 120), ("1h", 3600)):
            self.sleep.reset_mock()
            self.chat.side_effect = [True, False]
            main._loop_command(f"{text} hello", auto_apply=False)
            self.sleep.assert_called_once_with(seconds)

    def test_ctrl_c_during_the_wait_stops_the_loop(self):
        self.sleep.side_effect = KeyboardInterrupt()
        main._loop_command("5m hello", auto_apply=False)
        self.assertEqual(self.chat.call_count, 1)

    def test_stops_on_exception(self):
        self.chat.side_effect = [True, RuntimeError("boom")]
        main._loop_command("5 hello", auto_apply=False)
        self.assertEqual(self.chat.call_count, 2)

    def test_stops_when_budget_reached(self):
        state.session_budget = 1.0

        def spend(*args, **kwargs):
            state.session_cost += 0.6
            return True

        self.chat.side_effect = spend
        main._loop_command("5 hello", auto_apply=False)
        # After turn 1 the cost is 0.6 (under 1.0), after turn 2 it is 1.2: stop before turn 3.
        self.assertEqual(self.chat.call_count, 2)

    def test_queued_command_output_goes_with_the_first_turn_only(self):
        state.pending_context.append("I ran `make` in the project root.")
        main._loop_command("2 did it pass?", auto_apply=False)
        self.assertIn("make", self.chat.call_args_list[0].kwargs["context"])
        self.assertNotIn("context", self.chat.call_args_list[1].kwargs)

    def test_mentioned_file_is_read_again_for_every_turn(self):
        seen = iter(["first", "second"])

        def expand(text, root=None):
            return next(seen), ["a.py"], []

        with patch("llm_light.main.commands.expand_mentions", side_effect=expand):
            main._loop_command("2 look at @a.py", auto_apply=False)
        self.assertEqual([c.kwargs["context"] for c in self.chat.call_args_list],
                         ["first", "second"])

    def test_custom_command_is_expanded(self):
        with patch("llm_light.main.commands.expand_custom", return_value="Check the deploy.") as expand:
            main._loop_command("2 /deploy prod", auto_apply=False)
        expand.assert_called_with("/deploy prod", main._reserved_names())
        self.assertEqual(self.chat.call_args.args[0], "Check the deploy.")

    def test_builtin_or_unknown_command_is_not_sent_as_a_question(self):
        for question in ("/status", "/nosuchcommand"):
            main._loop_command(f"2 {question}", auto_apply=False)
        self.chat.assert_not_called()

    def test_not_logged_in_offers_login_and_stops(self):
        self.chat.side_effect = NotLoggedIn("log in")
        with patch("llm_light.main.providers.active") as active:
            main._loop_command("5 hello", auto_apply=False)
        active.return_value.offer_login.assert_called_once()
        self.assertEqual(self.chat.call_count, 1)

    def test_listed_in_help_and_the_slash_menu(self):
        self.assertIn("/loop", main._reserved_names())
        self.assertIn("/loop", {c.split()[0] for c, _ in main._all_commands()})

    def test_a_custom_command_named_loop_cannot_shadow_it(self):
        self.assertIsNone(commands.expand_custom("/loop", main._reserved_names()))


class TestLoopThroughRealChat(_LoopCase):
    """chat() itself, with only the provider call faked."""

    def _complete(self, *results):
        return patch.object(ClaudeProvider, "complete", side_effect=list(results)).start()

    def setUp(self):
        super().setUp()
        patch.object(ClaudeProvider, "turn_done").start()
        self._history = list(state.conversation_history)
        self._turns = state.turn_count
        self._checkpoints = list(state.checkpoints)

    def tearDown(self):
        state.conversation_history[:] = self._history
        state.turn_count = self._turns
        state.checkpoints[:] = self._checkpoints
        state.session_model = None
        super().tearDown()

    def test_completed_turns_all_run(self):
        complete = self._complete(*[("fine", None, True)] * 3)
        main._loop_command("3 hello", auto_apply=False)
        self.assertEqual(complete.call_count, 3)

    def test_ctrl_c_in_a_turn_stops_the_loop(self):
        # chat() catches the interrupt, so the loop only learns of it from chat's result.
        complete = self._complete(KeyboardInterrupt(), ("never", None, True))
        main._loop_command("5 hello", auto_apply=False)
        self.assertEqual(complete.call_count, 1)

    def test_failed_turn_stops_the_loop(self):
        complete = self._complete(RuntimeError("API down"), ("never", None, True))
        main._loop_command("5 hello", auto_apply=False)
        self.assertEqual(complete.call_count, 1)

    def test_chat_reports_how_the_turn_ended(self):
        from llm_light.llm import chat
        self._complete(("fine", None, True), KeyboardInterrupt(), RuntimeError("down"))
        self.assertTrue(chat("one", use_index=False))
        self.assertFalse(chat("two", use_index=False))
        self.assertFalse(chat("three", use_index=False))


if __name__ == "__main__":
    unittest.main()
