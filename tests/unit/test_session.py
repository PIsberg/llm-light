"""
Tests for llm_light.session: saving the conversation after each turn and
resuming it with --continue (issue #65).
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy-test-key")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import llm_light as cl
import llm_light.state as state
from llm_light import config, session

_HISTORY = [
    {"role": "user", "content": "What does OrderService do?"},
    {"role": "assistant", "content": "It places orders."},
]


class _SessionTest(unittest.TestCase):

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="cl_sessions_"))
        patcher = patch.object(config, "SESSION_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._reset_state()
        self.addCleanup(self._reset_state)

    def _reset_state(self):
        state.conversation_history.clear()
        state.turn_count = 0
        state.session_model = None

    def _fill_state(self):
        state.conversation_history.extend(dict(m) for m in _HISTORY)
        state.turn_count = 1
        state.session_model = "claude-haiku-5-5"


class TestSaveAndRestore(_SessionTest):

    def test_round_trip(self):
        self._fill_state()
        session.save()
        self._reset_state()
        message = session.restore()
        self.assertEqual(state.conversation_history, _HISTORY)
        self.assertEqual(state.turn_count, 1)
        self.assertEqual(state.session_model, "claude-haiku-5-5")
        self.assertIn("1 turn", message)

    def test_restore_fills_the_shared_list_in_place(self):
        # _wrap() re-binds state.conversation_history from the package global
        # before every wrapped call. A restore that assigned a new list would
        # be undone by the first chat() after --continue.
        self._fill_state()
        session.save()
        self._reset_state()
        session.restore()
        cl._sync_bindings()
        self.assertEqual(state.conversation_history, _HISTORY)

    def test_missing_file_starts_fresh(self):
        message = session.restore()
        self.assertEqual(state.conversation_history, [])
        self.assertIn("No previous conversation", message)

    def test_corrupt_file_starts_fresh(self):
        session.session_file().parent.mkdir(parents=True, exist_ok=True)
        session.session_file().write_text("{not json", encoding="utf-8")
        message = session.restore()
        self.assertEqual(state.conversation_history, [])
        self.assertIn("Could not read", message)

    def test_wrong_shape_is_rejected(self):
        bad_files = [
            [],
            {"conversation_history": "text"},
            {"conversation_history": [{"role": "system", "content": "x"}]},
            {"conversation_history": [{"role": "user", "content": 3}]},
            {"conversation_history": _HISTORY, "turn_count": "1"},
            {"conversation_history": _HISTORY, "session_model": 5},
        ]
        session.session_file().parent.mkdir(parents=True, exist_ok=True)
        for data in bad_files:
            session.session_file().write_text(json.dumps(data), encoding="utf-8")
            message = session.restore()
            self.assertEqual(state.conversation_history, [], data)
            self.assertIn("Could not read", message, data)

    def test_saving_an_empty_conversation_removes_the_file(self):
        self._fill_state()
        session.save()
        self.assertTrue(session.session_file().exists())
        state.conversation_history.clear()
        session.save()
        self.assertFalse(session.session_file().exists())

    def test_clear_removes_the_file(self):
        self._fill_state()
        session.save()
        session.clear()
        self.assertFalse(session.session_file().exists())
        session.clear()  # nothing to remove is fine

    def test_file_lives_outside_the_project(self):
        # A cloned repo could ship a crafted conversation in its own
        # .llm_light_cache; --continue must only read the user's own.
        with patch.object(config, "SESSION_DIR", Path.home() / ".llm_light_sessions"):
            path = session.session_file()
        self.assertNotIn(Path.cwd().resolve(), path.resolve().parents)

    def test_each_project_has_its_own_file(self):
        a = session.session_file(Path("/work/a"))
        b = session.session_file(Path("/work/b"))
        self.assertNotEqual(a, b)
        self.assertEqual(a, session.session_file(Path("/work/a")))

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits; Windows ignores them")
    def test_only_the_user_can_read_saved_conversations(self):
        # They quote the user's code. With the usual umask 022 the file and
        # directory would otherwise be readable by every local account.
        sessions = self.dir / "fresh"
        with patch.object(config, "SESSION_DIR", sessions):
            old = os.umask(0o022)
            try:
                self._fill_state()
                session.save()
            finally:
                os.umask(old)
            path = session.session_file()
        self.assertEqual(sessions.stat().st_mode & 0o077, 0)
        self.assertEqual(path.stat().st_mode & 0o077, 0)

    def test_save_failure_does_not_raise(self):
        self._fill_state()
        with patch.object(config, "SESSION_DIR", self.dir / "file"):
            (self.dir / "file").write_text("", encoding="utf-8")  # not a dir
            with patch("sys.stderr"):
                self.assertFalse(session.save())


class TestChatLoopSavesTheConversation(_SessionTest):

    def _run(self, inputs, resume=False):
        def fake_chat(query, **kwargs):
            state.conversation_history.extend([
                {"role": "user", "content": query},
                {"role": "assistant", "content": "answer"}])
            state.turn_count += 1

        with patch.object(cl, "full_refresh"), \
             patch("llm_light.Observer", return_value=MagicMock()), \
             patch("llm_light.SourceHandler", return_value=MagicMock()), \
             patch("threading.Thread", return_value=MagicMock()), \
             patch.object(cl, "_PROMPTTK_AVAILABLE", False), \
             patch.object(cl, "print_session_summary"), \
             patch.object(cl, "chat", side_effect=fake_chat), \
             patch("builtins.input", side_effect=inputs), \
             patch("builtins.print") as mock_print:
            cl.start_chat(resume=resume)
        return " ".join(str(a) for c in mock_print.call_args_list for a in c.args)

    def test_each_turn_is_saved(self):
        self._run(["first question", "exit"])
        saved = json.loads(session.session_file().read_text(encoding="utf-8"))
        self.assertEqual(saved["conversation_history"][0]["content"], "first question")
        self.assertEqual(saved["turn_count"], 1)

    def test_continue_restores_before_the_first_prompt(self):
        self._fill_state()
        session.save()
        self._reset_state()
        printed = self._run(["second question", "exit"], resume=True)
        self.assertIn("Continuing", printed)
        self.assertEqual([m["content"] for m in state.conversation_history],
                         [_HISTORY[0]["content"], _HISTORY[1]["content"],
                          "second question", "answer"])
        self.assertEqual(state.turn_count, 2)

    def test_without_continue_the_saved_conversation_is_not_loaded(self):
        self._fill_state()
        session.save()
        self._reset_state()
        self._run(["exit"])
        self.assertEqual(state.conversation_history, [])

    def test_clear_forgets_the_saved_conversation(self):
        self._fill_state()
        session.save()
        self._run(["/clear", "exit"])
        self.assertFalse(session.session_file().exists())


class TestContinueFlag(unittest.TestCase):

    def _main(self, argv, isatty=True):
        import llm_light.main as main_mod
        with patch.object(main_mod.sys, "argv", ["llm_light", *argv]), \
             patch.object(main_mod.sys.stdin, "isatty", return_value=isatty), \
             patch.object(main_mod, "_start_llmlingua_load"), \
             patch.object(main_mod, "start_chat") as start_chat, \
             patch.object(main_mod, "one_shot") as one_shot:
            main_mod.main()
        return start_chat, one_shot

    def test_continue_resumes_an_interactive_session(self):
        start_chat, one_shot = self._main(["--continue"])
        start_chat.assert_called_once_with(auto_apply=False, resume=True, pick=False)
        one_shot.assert_not_called()

    def test_plain_start_does_not_resume(self):
        start_chat, _ = self._main([])
        start_chat.assert_called_once_with(auto_apply=False, resume=False, pick=False)

    def test_resume_flag_asks_which_conversation(self):
        start_chat, _ = self._main(["--resume"])
        start_chat.assert_called_once_with(auto_apply=False, resume=False, pick=True)

    def test_resume_with_a_one_shot_question_is_a_usage_error(self):
        with patch("sys.stderr"), self.assertRaises(SystemExit) as raised:
            self._main(["--resume", "what next?"])
        self.assertEqual(raised.exception.code, 2)

    def test_continue_with_a_one_shot_question_is_a_usage_error(self):
        # One-shot turns send no history, so resuming one would do nothing.
        with patch("sys.stderr"), self.assertRaises(SystemExit) as raised:
            self._main(["--continue", "what next?"])
        self.assertEqual(raised.exception.code, 2)


class TestEarlierConversations(_SessionTest):
    """#136: /clear keeps the conversation as an archive, and /resume or
    --resume picks one of this project's earlier conversations."""

    def _save(self, *questions):
        state.conversation_history.clear()
        for q in questions:
            state.conversation_history += [{"role": "user", "content": q},
                                           {"role": "assistant", "content": "a"}]
        state.turn_count = len(questions)
        self.assertTrue(session.save())

    def test_clear_archives_the_conversation_instead_of_deleting_it(self):
        self._save("first question")
        session.clear()
        self.assertFalse(session.session_file().exists())
        entries = session.conversations()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["first"], "first question")
        self.assertFalse(entries[0]["current"])

    def test_conversations_lists_the_current_one_first_then_the_archives_newest_first(self):
        self._save("old one")
        session.clear()
        self._save("middle one", "second turn")
        session.clear()
        self._save("current one")
        entries = session.conversations()
        self.assertEqual([e["first"] for e in entries], ["current one", "middle one", "old one"])
        self.assertEqual([e["turns"] for e in entries], [1, 2, 1])
        self.assertEqual([e["current"] for e in entries], [True, False, False])

    def test_resume_swaps_an_archived_conversation_in_and_keeps_the_current_one(self):
        self._save("old one")
        session.clear()
        self._save("current one")
        old = next(e for e in session.conversations() if not e["current"])
        msg = session.resume(old["path"])
        self.assertIn("1 turn", msg)
        self.assertEqual(state.conversation_history[0]["content"], "old one")
        firsts = {e["first"]: e["current"] for e in session.conversations()}
        self.assertEqual(firsts, {"old one": True, "current one": False})

    def test_only_the_newest_archives_are_kept(self):
        for i in range(config.SESSION_KEEP + 3):
            self._save(f"conversation {i}")
            session.clear()
        entries = session.conversations()
        self.assertEqual(len(entries), config.SESSION_KEEP)
        self.assertEqual(entries[0]["first"], f"conversation {config.SESSION_KEEP + 2}")

    def test_archives_belong_to_their_provider(self):
        self._save("claude conversation")
        session.clear()
        gemini = MagicMock()
        gemini.name = "gemini"
        with patch.object(session.providers, "active", return_value=gemini):
            self.assertEqual(session.conversations(), [])

    def test_an_unreadable_archive_is_skipped(self):
        self._save("good one")
        session.clear()
        bad = session.session_file().with_name(session.session_file().stem + ".saved-19990101-000000.json")
        bad.write_text("{not json", encoding="utf-8")
        self.assertEqual([e["first"] for e in session.conversations()], ["good one"])


class TestResumeCommand(unittest.TestCase):

    def test_loop_resume_picks_a_conversation_and_resets_the_turn_state(self):
        import llm_light.main as main_mod
        state.checkpoints[:] = [{"question": "q", "history_len": 0, "head": None, "turn": 0}]
        state.replies[:] = ["r"]
        entries = [{"path": Path("x.json"), "label": "2026-11-02 10:00 · 1 turn · old"}]
        feed = iter(["/resume", "exit"])
        with patch.object(session, "conversations", return_value=entries), \
             patch.object(session, "resume", return_value="Continuing.") as resume, \
             patch.object(main_mod, "choose", return_value="0"), \
             patch.object(cl, "full_refresh"), \
             patch("llm_light.Observer", return_value=MagicMock()), \
             patch("llm_light.SourceHandler", return_value=MagicMock()), \
             patch("threading.Thread", return_value=MagicMock()), \
             patch.object(cl, "_PROMPTTK_AVAILABLE", False), \
             patch.object(cl, "print_session_summary"), \
             patch.object(cl.main.session, "save"), \
             patch("builtins.input", side_effect=lambda _p=">": next(feed)), \
             patch("builtins.print"):
            try:
                cl.start_chat()
            finally:
                state.stop_event.clear()
        resume.assert_called_once_with(Path("x.json"))
        self.assertEqual(state.checkpoints, [])
        self.assertEqual(state.replies, [])


if __name__ == "__main__":
    unittest.main()
