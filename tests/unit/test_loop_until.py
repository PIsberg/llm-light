"""/loop ... --until "<cmd>": stop when a command passes (#194). The checks here are real
commands, because what matters is what an exit code does to the loop."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy-test-key")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import llm_light.state as state
from llm_light import executor, main

_PY = Path(sys.executable).as_posix()


def _exits_when_file_exists(path) -> str:
    """A check that passes once `path` exists, as a script: no quotes to nest."""
    script = Path(path).with_name("check.py")
    script.write_text("import os, sys\nsys.exit(0 if os.path.exists(sys.argv[1]) else 1)\n", encoding="utf-8")
    return f"{_PY} {script.as_posix()} {Path(path).as_posix()}"


class _UntilCase(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.work = Path(tempfile.mkdtemp(prefix="cl_until_"))
        self.marker = self.work / "green"
        self.check = _exits_when_file_exists(self.marker)
        self.printed = []
        self.chat = patch("llm_light.main.chat", return_value=True).start()
        patch("llm_light.session.save").start()
        self.sleep = patch("llm_light.main.time.sleep").start()
        patch("llm_light.main._index_ready_for_query", return_value=False).start()
        patch("builtins.print", side_effect=lambda *a, **k: self.printed.append(" ".join(str(x) for x in a))).start()
        self.addCleanup(patch.stopall)
        self._budget = (state.session_budget, state.session_cost)
        state.session_budget, state.session_cost = None, 0.0

    def tearDown(self):
        state.session_budget, state.session_cost = self._budget
        state.pending_context.clear()

    def loop(self, arg):
        main._loop_command(arg, auto_apply=False)
        return "\n".join(self.printed)


class TestUntil(_UntilCase):

    def test_a_check_that_already_passes_spends_no_turn(self):
        self.marker.write_text("x")
        out = self.loop(f"5 --until '{self.check}' fix the tests")
        self.chat.assert_not_called()
        self.assertIn("passed", out)

    def test_stops_on_the_turn_that_makes_it_pass(self):
        turns = []

        def chat(query, **kwargs):
            turns.append(query)
            if len(turns) == 2:
                self.marker.write_text("x")
            return True

        self.chat.side_effect = chat
        out = self.loop(f"5 --until '{self.check}' fix the tests")
        self.assertEqual(turns, ["fix the tests", "fix the tests"])
        self.assertIn("passed", out)
        self.assertIn("2 turn", out)

    def test_the_turn_sees_the_failing_output(self):
        script = self.work / "fails.py"
        script.write_text("import sys\nprint('E: the failure text')\nsys.exit(1)\n", encoding="utf-8")
        seen = []

        def chat(query, **kwargs):
            seen.append(kwargs.get("context", ""))
            return True

        self.chat.side_effect = chat
        self.loop(f"1 --until '{_PY} {script.as_posix()}' fix it")
        self.assertIn("E: the failure text", seen[0])

    def test_says_when_it_never_passed(self):
        out = self.loop(f"2 --until '{self.check}' fix the tests")
        self.assertEqual(self.chat.call_count, 2)
        self.assertIn("still fail", out)

    def test_a_pass_on_the_last_turn_is_reported_as_a_pass(self):
        def chat(query, **kwargs):
            self.marker.write_text("x")
            return True

        self.chat.side_effect = chat
        out = self.loop(f"1 --until '{self.check}' fix the tests")
        self.assertIn("passed", out)
        self.assertNotIn("still fail", out)

    def test_a_check_that_cannot_run_stops_at_once(self):
        """It could never pass, so going on would spend every turn on a typo."""
        out = self.loop("5 --until 'no-such-command-xyz --flag' fix the tests")
        self.chat.assert_not_called()
        self.assertIn("could not run", out)

    def test_a_check_that_times_out_stops(self):
        with patch.object(executor, "_RUN_TIMEOUT_SECS", 0.5):
            out = self.loop(f"""5 --until '{_PY} -c "import time; time.sleep(30)"' fix the tests""")
        self.chat.assert_not_called()
        self.assertIn("timed out", out)

    def test_a_failed_turn_still_stops_the_loop(self):
        self.chat.return_value = False
        self.loop(f"5 --until '{self.check}' fix the tests")
        self.assertEqual(self.chat.call_count, 1)

    def test_ctrl_c_during_the_check_stops_the_loop(self):
        with patch.object(executor, "run_command_status", side_effect=KeyboardInterrupt):
            out = self.loop(f"5 --until '{self.check}' fix the tests")
        self.chat.assert_not_called()
        self.assertIn("stopped", out)

    def test_works_with_an_interval_and_waits_between_turns(self):
        out = self.loop(f"5m --until '{self.check}' fix the tests")
        self.assertEqual(self.chat.call_count, main.LOOP_MAX)
        self.sleep.assert_called_with(300)
        self.assertIn("still fail", out)

    def test_double_quotes_work_too(self):
        self.marker.write_text("x")
        self.loop(f'5 --until "{self.check}" fix')
        self.chat.assert_not_called()

    def test_a_single_word_needs_no_quotes(self):
        with patch.object(executor, "run_command_status", return_value=("ok", 0)) as run:
            self.loop("5 --until make fix the build")
        self.assertEqual(run.call_args.args[0], "make")

    def test_usage_on_a_missing_question_or_command(self):
        for arg in ("5 --until 'make'", "5 --until", "5 --until ''  fix"):
            self.printed.clear()
            self.loop(arg)
            self.chat.assert_not_called()
            self.assertIn("Usage", "\n".join(self.printed))

    def test_without_until_nothing_runs(self):
        with patch.object(executor, "run_command_status") as run:
            self.loop("2 a question")
        run.assert_not_called()
        self.assertEqual(self.chat.call_count, 2)


class TestRunCommandStatus(unittest.TestCase):

    def setUp(self):
        patch("builtins.print").start()
        self.addCleanup(patch.stopall)

    def test_returns_the_exit_code(self):
        transcript, code = executor.run_command_status(f'{_PY} -c "raise SystemExit(3)"')
        self.assertEqual(code, 3)
        self.assertIn("(exit 3)", transcript)

    def test_the_transcript_is_what_run_command_returns(self):
        command = f'{_PY} -c "print(42)"'
        self.assertEqual(executor.run_command_status(command)[0], executor._run_command(command))

    def test_a_timeout_has_no_exit_code(self):
        with patch.object(executor, "_RUN_TIMEOUT_SECS", 0.5):
            _, code = executor.run_command_status(f'{_PY} -c "import time; time.sleep(30)"')
        self.assertIsNone(code)


if __name__ == "__main__":
    unittest.main()
