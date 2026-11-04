"""Subscription turns continue one CLI session instead of re-sending the history.

Flattened into one text block, the history had no cache boundary of its own, so
every turn wrote all of it again, and the files the agent read in earlier turns
were gone, so it read them again. These tests run the shipped call against a real
child process standing in for the CLI, and look at what that child received.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-mock-key")

import llm_light.state as state  # noqa: E402
from llm_light import commands, config, llm  # noqa: E402
from llm_light.providers import claude  # noqa: E402

_FAKE_CLI = r"""
import json, os, sys
log, args = sys.argv[1], sys.argv[2:]
rows = [json.loads(line) for line in open(log, encoding="utf-8")] if os.path.exists(log) else []
system = ""
if "--system-prompt-file" in args:
    system = open(args[args.index("--system-prompt-file") + 1], encoding="utf-8").read()
home = os.environ["HOME"]
row = {"args": args, "prompt": sys.stdin.buffer.read().decode("utf-8"), "home": home, "system": system,
       "resume": args[args.index("--resume") + 1] if "--resume" in args else None}
with open(log, "a", encoding="utf-8") as f:
    f.write(json.dumps(row) + "\n")
mode = open(log + ".mode").read().strip() if os.path.exists(log + ".mode") else ""
if row["resume"] and mode == "resume-refused":
    print("No conversation found with session ID", file=sys.stderr)
    sys.exit(1)
if row["resume"] and mode == "resume-dies-after-output":
    print(json.dumps({"type": "stream_event", "event": {"type": "content_block_delta",
          "delta": {"type": "text_delta", "text": "partial"}}}), flush=True)
    sys.exit(1)
session = row["resume"] or f"sess-{len(rows)}"
# As claude 2.1.316 reports them: usage is this run's, total_cost_usd the session's.
runs = 1 + sum(1 for r in rows if r["resume"] == session or (r["resume"] is None and row["resume"] == f"sess-{rows.index(r)}"))
print(json.dumps({"type": "result", "subtype": "success", "result": f"reply {len(rows)}",
                  "usage": {"input_tokens": 1, "output_tokens": 1}, "session_id": "" if mode == "no-session-id" else session,
                  "total_cost_usd": round(0.01 * runs, 4)}), flush=True)
"""


def _text(text):
    return {"type": "text", "text": text}


def _request(question, history=(), model=config.MODEL_SONNET):
    messages = [{"role": r, "content": c} for r, c in history]
    messages.append({"role": "user", "content": [_text(question)]})
    return dict(model=model, max_tokens=100, system=[_text("SYS")], messages=messages)


class _CliCase(unittest.TestCase):
    def setUp(self):
        work = tempfile.mkdtemp(prefix="cl_session_")
        self.addCleanup(shutil.rmtree, work, True)
        self.log = os.path.join(work, "calls.jsonl")
        script = os.path.join(work, "cli.py")
        Path(script).write_text(_FAKE_CLI, encoding="utf-8")
        real_popen = subprocess.Popen

        def popen(cmd, *args, **kwargs):
            if cmd[0] not in ("claude", sys.executable):  # git, from chat()'s checkpoint
                return real_popen(cmd, *args, **kwargs)
            return real_popen([sys.executable, script, self.log, *cmd[1:]], *args, **kwargs)

        for p in (patch("subprocess.Popen", side_effect=popen),
                  patch("shutil.which", return_value=sys.executable),
                  patch.object(config, "AUTH_MODE", "OAUTH"),
                  patch.object(claude, "AUTH_MODE", "OAUTH"),
                  patch.object(llm, "AUTH_MODE", "OAUTH"),
                  patch("sys.stdout", io.StringIO()),
                  patch("sys.stderr", io.StringIO())):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(claude._remove_cli_home)

    def turn(self, question, history=(), model=config.MODEL_SONNET):
        return claude._make_streaming_api_call(**_request(question, history, model))

    def mode(self, mode):
        Path(self.log + ".mode").write_text(mode, encoding="utf-8")

    def calls(self):
        return [json.loads(line) for line in open(self.log, encoding="utf-8")]


class TestResume(_CliCase):
    def test_the_first_turn_is_the_whole_prompt_with_no_resume(self):
        self.turn("first question", history=[("user", "older question"), ("assistant", "older answer")])
        call = self.calls()[0]
        self.assertIsNone(call["resume"])
        self.assertIn("older answer", call["prompt"])
        self.assertIn("first question", call["prompt"])

    def test_the_next_turn_resumes_and_sends_only_the_new_message(self):
        self.turn("first question")
        self.turn("second question", history=[("user", "first question"), ("assistant", "reply 0")])
        call = self.calls()[1]
        self.assertEqual(call["resume"], "sess-0")
        self.assertIn("second question", call["prompt"])
        self.assertNotIn("first question", call["prompt"])
        self.assertNotIn("reply 0", call["prompt"])

    def test_a_resumed_turn_still_gets_the_current_skeleton(self):
        # By default the CLI records the first system prompt and resends that record
        # on every resume, so a skeleton rebuilt after an edit would never arrive.
        self.turn("first question")
        request = _request("second question")
        request["system"] = [_text("SYS after a file changed")]
        claude._make_streaming_api_call(**request)
        call = self.calls()[1]
        self.assertEqual(call["system"], "SYS after a file changed")
        at = call["args"].index("--system-prompt-snapshot")
        self.assertEqual(call["args"][at + 1], "off")

    def test_a_lint_retry_sends_only_the_error(self):
        # chat() appends the reply and the error, and the session already has the reply.
        self.turn("first question")
        self.turn("[Error] fix the syntax", history=[("user", "first question"), ("assistant", "bad edit")])
        call = self.calls()[1]
        self.assertEqual(call["resume"], "sess-0")
        self.assertNotIn("bad edit", call["prompt"])
        self.assertIn("[Error] fix the syntax", call["prompt"])

    def test_a_turn_on_another_model_starts_over_with_the_whole_prompt(self):
        self.turn("first question")
        self.turn("second question", history=[("user", "first question"), ("assistant", "reply 0")],
                  model=config.MODEL_OPUS)
        call = self.calls()[1]
        self.assertIsNone(call["resume"])
        self.assertIn("first question", call["prompt"])

    def test_a_forgotten_session_starts_over_with_the_whole_prompt(self):
        self.turn("first question")
        state.cli_session = None
        self.turn("second question", history=[("user", "first question"), ("assistant", "reply 0")])
        call = self.calls()[1]
        self.assertIsNone(call["resume"])
        self.assertIn("first question", call["prompt"])

    def test_a_result_without_a_session_id_keeps_the_whole_prompt(self):
        self.mode("no-session-id")
        self.turn("first question")
        self.turn("second question", history=[("user", "first question"), ("assistant", "reply 0")])
        self.assertIsNone(self.calls()[1]["resume"])

    def test_a_session_id_that_is_not_an_id_is_not_put_on_the_command_line(self):
        # Windows runs the CLI through cmd.exe.
        claude._remember_session("x & calc", config.MODEL_SONNET, type("U", (), {"cost_usd": None})())
        self.assertIsNone(state.cli_session)

    def test_opting_out_never_resumes_and_keeps_one_home_per_call(self):
        with patch.object(config, "CLI_RESUME", False):
            self.turn("first question")
            self.turn("second question", history=[("user", "first question"), ("assistant", "reply 0")])
        first, second = self.calls()
        self.assertIsNone(second["resume"])
        self.assertIn("first question", second["prompt"])
        self.assertNotEqual(first["home"], second["home"])
        self.assertFalse(os.path.exists(first["home"]))


class _RepoCase(_CliCase):
    """In a real git repository, since what changed is git's answer."""

    def setUp(self):
        super().setUp()
        repo = tempfile.mkdtemp(prefix="cl_repo_")
        # git's object files are read-only, which stops rmtree on Windows.
        self.addCleanup(shutil.rmtree, repo, onexc=lambda rm, path, _: (os.chmod(path, 0o700), rm(path)))
        previous = os.getcwd()
        os.chdir(repo)
        self.addCleanup(os.chdir, previous)
        self.git("init", "-q")
        Path("kept.py").write_text("KEPT = 1\n", encoding="utf-8")
        Path("other.py").write_text("OTHER = 1\n", encoding="utf-8")
        self.git("add", ".")
        self.commit("first")

    def git(self, *args):
        return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout

    def commit(self, message):
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", message)


class TestChangedFiles(_RepoCase):
    def _turn_after(self, change):
        self.turn("first question")
        change()
        self.turn("second question")
        return self.calls()[1]

    def test_a_file_edited_between_turns_is_named(self):
        call = self._turn_after(lambda: Path("kept.py").write_text("KEPT = 2\n", encoding="utf-8"))
        self.assertEqual(call["resume"], "sess-0")
        self.assertIn("kept.py", call["prompt"])
        self.assertIn("out of date", call["prompt"])
        self.assertNotIn("other.py", call["prompt"])

    def test_a_dirty_file_edited_again_is_named(self):
        Path("kept.py").write_text("KEPT = 2\n", encoding="utf-8")
        call = self._turn_after(lambda: Path("kept.py").write_text("KEPT = 3333\n", encoding="utf-8"))
        self.assertIn("kept.py", call["prompt"])

    def test_a_dirty_file_restored_between_turns_is_named(self):
        Path("kept.py").write_text("KEPT = 2\n", encoding="utf-8")
        call = self._turn_after(lambda: self.git("checkout", "--", "kept.py"))
        self.assertIn("kept.py", call["prompt"])

    def test_a_new_file_is_named(self):
        call = self._turn_after(lambda: Path("new.py").write_text("NEW = 1\n", encoding="utf-8"))
        self.assertIn("new.py", call["prompt"])

    def test_a_commit_between_turns_names_its_files(self):
        def change():
            Path("kept.py").write_text("KEPT = 2\n", encoding="utf-8")
            self.git("add", ".")
            self.commit("second")
        call = self._turn_after(change)
        self.assertIn("kept.py", call["prompt"])

    def test_an_unchanged_tree_adds_nothing(self):
        call = self._turn_after(lambda: None)
        self.assertEqual(call["resume"], "sess-0")
        self.assertNotIn("out of date", call["prompt"])

    def test_a_long_list_is_cut_short(self):
        def change():
            for i in range(30):
                Path(f"new{i:02}.py").write_text("x = 1\n", encoding="utf-8")
        self.git("add", ".")
        call = self._turn_after(change)
        self.assertIn("and 10 more", call["prompt"])

    def test_a_lint_retry_does_not_repeat_the_list(self):
        self.turn("first question")
        Path("kept.py").write_text("KEPT = 2\n", encoding="utf-8")
        self.turn("second question")
        self.turn("[Error] fix the syntax")
        self.assertNotIn("kept.py", self.calls()[2]["prompt"])

    def test_outside_a_repository_every_turn_starts_over(self):
        # Nothing could say a remembered file is out of date, so nothing is remembered.
        plain = tempfile.mkdtemp(prefix="cl_plain_")
        self.addCleanup(shutil.rmtree, plain, True)
        os.chdir(plain)
        self.turn("first question")
        self.turn("second question", history=[("user", "first question"), ("assistant", "reply 0")])
        call = self.calls()[1]
        self.assertIsNone(call["resume"])
        self.assertIn("first question", call["prompt"])

    def test_a_rewritten_head_starts_over(self):
        self.turn("first question")
        state.cli_tree = {"head": "0" * 40, "files": {}}
        self.turn("second question", history=[("user", "first question"), ("assistant", "reply 0")])
        self.assertIsNone(self.calls()[1]["resume"])


class TestIdle(_CliCase):
    """Past the cache entry's life a resume writes the whole session again. After 65 idle
    minutes a one-word turn wrote 51,583 tokens and cost $0.2066; on the whole prompt the
    same turn wrote 4,680 and cost $0.0188 (claude 2.1.316)."""

    def _turn_after(self, idle_secs):
        self.turn("first question")
        state.cli_session["at"] -= idle_secs
        self.turn("second question", history=[("user", "first question"), ("assistant", "reply 0")])
        return self.calls()[1]

    def test_a_turn_after_the_entry_expires_starts_over_with_the_whole_prompt(self):
        call = self._turn_after(config.CACHE_TTL_SECS + 1)
        self.assertIsNone(call["resume"])
        self.assertIn("first question", call["prompt"])
        self.assertIn("second question", call["prompt"])

    def test_a_turn_inside_the_entrys_life_still_resumes(self):
        call = self._turn_after(config.CACHE_TTL_SECS - 60)
        self.assertEqual(call["resume"], "sess-0")
        self.assertNotIn("first question", call["prompt"])

    def test_every_turn_restarts_the_clock(self):
        # A read renews the entry, so a long session of short gaps never starts over.
        self.turn("first question")
        state.cli_session["at"] -= config.CACHE_TTL_SECS - 60
        self.turn("second question")
        state.cli_session["at"] -= config.CACHE_TTL_SECS - 60
        self.turn("third question")
        self.assertEqual(self.calls()[2]["resume"], "sess-0")

    def test_starting_over_charges_the_new_session_from_zero(self):
        self.turn("first question")
        state.cli_session["at"] -= config.CACHE_TTL_SECS + 1
        _, usage, _ = self.turn("second question")
        self.assertAlmostEqual(usage.cost_usd, 0.01)


class TestCost(_CliCase):
    def test_a_resumed_turn_is_charged_only_its_own_cost(self):
        # total_cost_usd counts the whole session, so adding it per turn over-reports:
        # 0.0463 + 0.0635 + 0.0914 ... on a live session whose last figure was the total.
        _, first, _ = self.turn("first question")
        _, second, _ = self.turn("second question")
        _, third, _ = self.turn("third question")
        self.assertAlmostEqual(first.cost_usd, 0.01)
        self.assertAlmostEqual(second.cost_usd, 0.01)
        self.assertAlmostEqual(third.cost_usd, 0.01)

    def test_a_new_session_starts_counting_again(self):
        self.turn("first question")
        state.cli_session = None
        _, usage, _ = self.turn("second question")
        self.assertAlmostEqual(usage.cost_usd, 0.01)


class TestHome(_CliCase):
    def test_the_session_keeps_its_home_between_turns(self):
        self.turn("first question")
        self.turn("second question")
        first, second = self.calls()
        self.assertEqual(first["home"], second["home"])
        self.assertTrue(os.path.isdir(first["home"]))

    def test_no_copy_of_the_credentials_outlives_a_call(self):
        fake_user = tempfile.mkdtemp(prefix="cl_user_")
        self.addCleanup(shutil.rmtree, fake_user, True)
        creds = Path(fake_user) / ".claude" / ".credentials.json"
        creds.parent.mkdir()
        creds.write_text("{}", encoding="utf-8")
        with patch("os.path.expanduser", return_value=fake_user):
            self.turn("first question")
        home = self.calls()[0]["home"]
        self.assertFalse((Path(home) / ".claude" / ".credentials.json").exists())

    def test_the_home_is_removed_when_the_run_ends(self):
        self.turn("first question")
        home = self.calls()[0]["home"]
        claude._remove_cli_home()
        self.assertFalse(os.path.exists(home))

    def test_a_hidden_call_stays_out_of_the_session(self):
        # The history summary may quote repo text and runs with no tools: it must not
        # join the conversation, or the agent would resume into a tool-free transcript.
        self.turn("first question")
        with patch.object(claude, "AUTH_MODE", "OAUTH"):
            claude.PROVIDER.summarize("summarize this")
        summary = self.calls()[1]
        self.assertIsNone(summary["resume"])
        self.assertNotEqual(summary["home"], self.calls()[0]["home"])
        self.assertEqual(state.cli_session["id"], "sess-0")


class TestResumeFailure(_CliCase):
    def test_a_refused_resume_runs_the_turn_on_the_whole_prompt(self):
        self.turn("first question")
        self.mode("resume-refused")
        reply, _, _ = self.turn("second question", history=[("user", "first question"), ("assistant", "reply 0")])
        refused, whole = self.calls()[1:]
        self.assertEqual(refused["resume"], "sess-0")
        self.assertIsNone(whole["resume"])
        self.assertIn("first question", whole["prompt"])
        self.assertIn("second question", whole["prompt"])
        self.assertEqual(reply, "reply 2")

    def test_after_a_refused_resume_the_run_stops_resuming(self):
        self.turn("first question")
        self.mode("resume-refused")
        self.turn("second question", history=[("user", "first question"), ("assistant", "reply 0")])
        self.turn("third question", history=[("user", "second question"), ("assistant", "reply 2")])
        self.assertIsNone(self.calls()[-1]["resume"])
        self.assertEqual(len(self.calls()), 4)

    def test_a_resume_that_failed_after_output_is_not_repeated(self):
        # It may have edited files by then: running the turn again could do it twice.
        self.turn("first question")
        self.mode("resume-dies-after-output")
        with self.assertRaises(RuntimeError):
            self.turn("second question")
        self.assertEqual(len(self.calls()), 2)


class TestForgetting(unittest.TestCase):
    """Anything that changes the conversation behind the session's back has to end
    the session, or the model keeps answering from turns the user took back."""

    def setUp(self):
        state.cli_session = {"id": "sess-0", "model": config.MODEL_SONNET, "at": 0.0, "cost": 0.0}
        self.addCleanup(setattr, state, "cli_session", None)
        self.saved_history = list(state.conversation_history)
        self.addCleanup(lambda: state.conversation_history.__setitem__(slice(None), self.saved_history))

    def _history(self, turns):
        state.conversation_history[:] = [
            {"role": role, "content": f"{role} {i}"} for i in range(turns) for role in ("user", "assistant")]

    def test_compressing_the_history_ends_the_session(self):
        self._history(config.MAX_HISTORY_TURNS + 1)
        with patch.object(llm, "_summarize_turns", return_value=("summary", None)):
            llm._maybe_compress_history()
        self.assertIsNone(state.cli_session)

    def test_a_failed_compression_ends_the_session(self):
        self._history(config.MAX_HISTORY_TURNS + 1)
        with patch.object(llm, "_summarize_turns", side_effect=RuntimeError("no")), \
             patch("sys.stdout", io.StringIO()):
            llm._maybe_compress_history()
        self.assertIsNone(state.cli_session)

    def test_a_short_history_keeps_the_session(self):
        self._history(2)
        llm._maybe_compress_history()
        self.assertIsNotNone(state.cli_session)

    def test_compact_ends_the_session(self):
        self._history(2)
        with patch.object(llm, "_summarize_turns", return_value=("summary", None)):
            llm.compact_history()
        self.assertIsNone(state.cli_session)

    def test_rewind_ends_the_session(self):
        self._history(2)
        state.checkpoints[:] = [{"question": "q", "history_len": 0, "head": None, "turn": 0}]
        self.addCleanup(state.checkpoints.clear)
        with patch.object(commands.session, "save"):
            commands.rewind(0)
        self.assertIsNone(state.cli_session)


class TestChatTurn(_RepoCase):
    """Through chat(), which is what a user runs."""

    def setUp(self):
        super().setUp()
        self.saved_history = list(state.conversation_history)
        state.conversation_history.clear()
        state.pinned_model = config.MODEL_SONNET
        state.skeleton_context = "SKELETON"
        for p in (patch.object(llm, "_git_modified_snapshot", return_value=set()),
                  patch.object(llm, "record_retrieval"),
                  patch.object(commands.session, "save")):
            p.start()
            self.addCleanup(p.stop)

        def restore():
            state.conversation_history[:] = self.saved_history
            state.cli_session = None
            state.pinned_model = None

        self.addCleanup(restore)

    def test_two_turns_share_one_session(self):
        llm.chat("first question", use_index=False)
        llm.chat("second question", use_index=False)
        first, second = self.calls()
        self.assertIsNone(first["resume"])
        self.assertEqual(second["resume"], "sess-0")
        self.assertNotIn("first question", second["prompt"])
        self.assertEqual(len(state.conversation_history), 4)

    def test_an_edit_made_during_the_turn_is_not_reported_as_outside(self):
        # The session made it. Naming it would send the agent to read it again.
        real_apply = claude.PROVIDER.complete

        def complete_and_edit(request):
            result = real_apply(request)
            Path("kept.py").write_text("KEPT = 2\n", encoding="utf-8")
            return result

        with patch.object(claude.PROVIDER, "complete", side_effect=complete_and_edit), \
             patch.object(llm, "_commit_agent_edits"):
            llm.chat("first question", use_index=False)
        llm.chat("second question", use_index=False)
        self.assertEqual(self.calls()[1]["resume"], "sess-0")
        self.assertNotIn("out of date", self.calls()[1]["prompt"])

    def test_a_failed_turn_ends_the_session(self):
        # The turn is not in the history, so the session and the history disagree.
        llm.chat("first question", use_index=False)
        self.mode("resume-dies-after-output")
        llm.chat("second question", use_index=False)
        self.assertIsNone(state.cli_session)
        self.assertEqual(len(state.conversation_history), 2)

    def test_an_interrupted_turn_ends_the_session(self):
        llm.chat("first question", use_index=False)
        with patch.object(claude.PROVIDER, "complete", side_effect=KeyboardInterrupt):
            llm.chat("second question", use_index=False)
        self.assertIsNone(state.cli_session)

    def test_lint_retries_that_run_out_end_the_session(self):
        llm.chat("first question", use_index=False)
        edit = "file.py\n<<<<<<< SEARCH\n=======\nnew\n>>>>>>> REPLACE\n"
        with patch.object(claude.PROVIDER, "complete",
                          return_value=(edit, type("U", (), {})(), True)), \
             patch.object(llm, "parse_edit_blocks", return_value=[{"path": "file.py"}]), \
             patch.object(llm, "apply_edits", return_value=["file.py: syntax error"]), \
             patch.object(llm, "_record_usage", return_value=False):
            llm.chat("second question", use_index=False)
        self.assertIsNone(state.cli_session)
        self.assertEqual(len(state.conversation_history), 2)


if __name__ == "__main__":
    unittest.main()
