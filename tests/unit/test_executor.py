"""
Tests for llm_light.executor — _run_command and auto_tune.
"""

import _thread
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy-test-key")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from llm_light.executor import _run_command, _RUN_HEAD_LINES, _RUN_TAIL_LINES, _RUN_MAX_CHARS, auto_tune
from llm_light.providers.runner import kill_process_tree
import llm_light.state as _state


class TestRunCommand(unittest.TestCase):
    """Tests for shell command execution."""

    def test_run_success(self):
        print("\n  ▶ TestRunCommand.test_run_success")
        result = _run_command('python -c "print(\'hello\')"')
        self.assertIn("exit 0", result)
        self.assertIn("hello", result)

    def test_run_failure_exit_code(self):
        print("\n  ▶ TestRunCommand.test_run_failure_exit_code")
        result = _run_command('python -c "raise SystemExit(1)"')
        self.assertIn("exit 1", result)

    def test_run_truncation(self):
        print("\n  ▶ TestRunCommand.test_run_truncation")
        total = _RUN_HEAD_LINES + _RUN_TAIL_LINES + 50
        result = _run_command(f'python -c "[print(i) for i in range({total})]"')
        self.assertIn("lines omitted", result)

    def test_run_stderr_merged(self):
        print("\n  ▶ TestRunCommand.test_run_stderr_merged")
        result = _run_command('python -c "import sys; sys.stderr.write(\'err_token\\n\')"')
        self.assertIn("err_token", result)

    def test_run_outputs_stderr(self):
        print("\n  ▶ TestRunCommand.test_run_outputs_stderr")
        result = _run_command('python -c "import sys; sys.stderr.write(\'STDERROUT\\n\')"')
        self.assertIn("STDERROUT", result)

    def test_run_char_truncation(self):
        print("\n  ▶ TestRunCommand.test_run_char_truncation")
        result = _run_command(f'python -c "print(\'x\' * {_RUN_MAX_CHARS + 1000})"')
        self.assertIn("truncated", result)

    def test_run_includes_exit_code_in_transcript(self):
        print("\n  ▶ TestRunCommand.test_run_includes_exit_code_in_transcript")
        result = _run_command('python -c "raise SystemExit(42)"')
        self.assertIn("42", result)

    def test_run_times_out(self):
        print("\n  ▶ TestRunCommand.test_run_times_out")
        # Force a tiny timeout so a sleeping command is killed instead of
        # blocking the chat loop forever.
        with patch("llm_light.executor._RUN_TIMEOUT_SECS", 1):
            result = _run_command('python -c "import time; time.sleep(30)"')
        self.assertIn("TIMEOUT", result)

    def test_run_timeout_surfaces_partial_output(self):
        print("\n  ▶ TestRunCommand.test_run_timeout_surfaces_partial_output")
        with patch("llm_light.executor._RUN_TIMEOUT_SECS", 2):
            result = _run_command(
                'python -c "import time,sys; '
                'print(\'PARTIAL_TOKEN\', flush=True); time.sleep(30)"'
            )
        self.assertIn("TIMEOUT", result)
        self.assertIn("PARTIAL_TOKEN", result)

    def test_timeout_kills_the_child_of_the_shell(self):
        # shell=True: killing the shell alone leaves its child holding the pipe,
        # so on Windows the read waited out the child's whole 30 s.
        with patch("llm_light.executor._RUN_TIMEOUT_SECS", 1):
            started = time.monotonic()
            result = _run_command('python -c "import time; time.sleep(30)"')
        self.assertIn("TIMEOUT", result)
        self.assertLess(time.monotonic() - started, 15)

    def test_output_is_shown_while_the_command_runs(self):
        shown = []
        with patch("builtins.print", side_effect=lambda *a, **k: shown.append((time.monotonic(), a))):
            _run_command('python -c "import time; print(\'FIRST\', flush=True); time.sleep(2); print(\'LAST\')"')
        first = next(t for t, args in shown if args and str(args[0]).startswith("FIRST"))
        last = next(t for t, args in shown if args and str(args[0]).startswith("LAST"))
        self.assertGreater(last - first, 1)

    def test_python_child_output_is_not_held_back_by_its_buffer(self):
        # A piped Python buffers stdout, so without flush= nothing arrived until it ended.
        shown = []
        with patch("builtins.print", side_effect=lambda *a, **k: shown.append((time.monotonic(), a))):
            _run_command('python -c "import time; print(\'FIRST\'); time.sleep(2); print(\'LAST\')"')
        first = next(t for t, args in shown if args and str(args[0]).startswith("FIRST"))
        last = next(t for t, args in shown if args and str(args[0]).startswith("LAST"))
        self.assertGreater(last - first, 1)

    def test_ctrl_c_stops_the_command_and_its_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "still-ran"
            script = Path(tmp) / "slow.py"
            script.write_text(f"import time\ntime.sleep(3)\nopen({str(marker)!r}, 'w').close()\n")
            threading.Timer(0.7, _thread.interrupt_main).start()
            started = time.monotonic()
            with self.assertRaises(KeyboardInterrupt):
                _run_command(f'python "{script}"')
            self.assertLess(time.monotonic() - started, 2.5)
            time.sleep(3)
            self.assertFalse(marker.exists(), "the child kept running after Ctrl+C")

    def test_command_does_not_wait_on_our_stdin(self):
        # Run in a child with an open stdin pipe, as at a real prompt: a command
        # reading its input would otherwise wait forever, with nothing on screen.
        code = ("from llm_light.executor import _run_command;"
                "print(_run_command('python -c \"input()\"'))")
        proc = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace",
                                cwd=str(Path(__file__).resolve().parent.parent.parent))
        try:
            # wait(), not communicate(): that closes the stdin this test keeps open.
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            kill_process_tree(proc)
            self.fail("the command waited on stdin")
        finally:
            proc.stdin.close()
        self.assertIn("EOFError", proc.stdout.read())
        proc.stdout.close()

    def test_background_child_holding_the_pipe_does_not_block(self):
        # `!server &`: the shell is done, but its child keeps stdout open for 30 s.
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "spawn.py"
            script.write_text(
                "import subprocess, sys\n"
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(8)'])\n"
                "print('SPAWNED', flush=True)\n")
            started = time.monotonic()
            result = _run_command(f'python "{script}"')
        self.assertIn("SPAWNED", result)
        self.assertIn("(exit 0)", result)
        self.assertLess(time.monotonic() - started, 6)

    def test_chatty_command_still_times_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "chatty.py"
            script.write_text("import time\nwhile True:\n    print(1, flush=True)\n    time.sleep(0.01)\n")
            with patch("llm_light.executor._RUN_TIMEOUT_SECS", 1):
                started = time.monotonic()
                result = _run_command(f'python "{script}"')
        self.assertIn("TIMEOUT", result)
        self.assertLess(time.monotonic() - started, 15)


class TestAutoTune(unittest.TestCase):
    """Tests for embedding model auto-selection."""

    def _make_files(self, n):
        files = []
        for i in range(n):
            f = MagicMock()
            f.exists.return_value = True
            f.stat.return_value.st_size = 2000
            files.append(f)
        return files

    def test_small_repo_selects_minilm(self):
        print("\n  ▶ TestAutoTune.test_small_repo_selects_minilm")
        files = self._make_files(10)
        _state.EMBED_MODEL = None
        _state.embedder = None
        with patch("llm_light.executor.SentenceTransformer") as mock_st, \
             patch("llm_light.executor._Spinner"):
            mock_st.return_value = MagicMock()
            auto_tune(files, quiet=True)
        self.assertEqual(_state.EMBED_MODEL, "all-MiniLM-L6-v2")

    def test_medium_repo_selects_mpnet(self):
        print("\n  ▶ TestAutoTune.test_medium_repo_selects_mpnet")
        files = self._make_files(100)
        _state.EMBED_MODEL = None
        _state.embedder = None
        with patch("llm_light.executor.SentenceTransformer") as mock_st, \
             patch("llm_light.executor._Spinner"):
            mock_st.return_value = MagicMock()
            auto_tune(files, quiet=True)
        self.assertEqual(_state.EMBED_MODEL, "all-mpnet-base-v2")

    def test_large_repo_selects_nomic(self):
        print("\n  ▶ TestAutoTune.test_large_repo_selects_nomic")
        files = self._make_files(250)
        _state.EMBED_MODEL = None
        _state.embedder = None
        with patch("llm_light.executor.SentenceTransformer") as mock_st, \
             patch("llm_light.executor._Spinner"):
            mock_st.return_value = MagicMock()
            auto_tune(files, quiet=True)
        self.assertEqual(_state.EMBED_MODEL, "nomic-ai/nomic-embed-text-v1.5")

    def test_sets_top_k_from_chunks(self):
        print("\n  ▶ TestAutoTune.test_sets_top_k_from_chunks")
        files = self._make_files(5)
        chunks = [{"text": "a" * 400} for _ in range(5)]
        _state.EMBED_MODEL = None
        _state.embedder = None
        with patch("llm_light.executor.SentenceTransformer") as mock_st, \
             patch("llm_light.executor._Spinner"):
            mock_st.return_value = MagicMock()
            auto_tune(files, chunks=chunks, quiet=True)
        self.assertIsNotNone(_state.TOP_K)
        self.assertGreaterEqual(_state.TOP_K, 2)
        self.assertLessEqual(_state.TOP_K, 15)

    def test_reuses_embedder_if_model_unchanged(self):
        print("\n  ▶ TestAutoTune.test_reuses_embedder_if_model_unchanged")
        files = self._make_files(10)
        mock_embedder = MagicMock()
        _state.EMBED_MODEL = "all-MiniLM-L6-v2"
        _state.embedder = mock_embedder
        with patch("llm_light.executor.SentenceTransformer") as mock_st:
            auto_tune(files, quiet=True)
            mock_st.assert_not_called()
        self.assertIs(_state.embedder, mock_embedder)

    def test_top_k_with_no_chunks(self):
        print("\n  ▶ TestAutoTune.test_top_k_with_no_chunks")
        files = self._make_files(20)
        _state.EMBED_MODEL = None
        _state.embedder = None
        with patch("llm_light.executor.SentenceTransformer") as mock_st:
            mock_st.return_value = MagicMock()
            auto_tune(files, chunks=None, quiet=True)
        self.assertIsNotNone(_state.TOP_K)


if __name__ == "__main__":
    unittest.main()
