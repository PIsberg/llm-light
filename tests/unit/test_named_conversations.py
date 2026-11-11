"""#210: name a conversation with /rename, see the name in /resume, and return to it with
--resume <name> without being asked. A named conversation outlives SESSION_KEEP."""

import json
import os
import sys
from pathlib import Path
from unittest.mock import ANY, patch

import pytest

import llm_light.state as state
from llm_light import commands, config, session


@pytest.fixture(autouse=True)
def sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path / "sessions")

    def reset():
        state.conversation_history.clear()
        state.turn_count = 0
        state.session_model = None
        state.session_name = None
    reset()
    yield
    reset()


def _save(*questions):
    state.conversation_history.clear()
    for q in questions:
        state.conversation_history += [{"role": "user", "content": q},
                                       {"role": "assistant", "content": "a"}]
    state.turn_count = len(questions)
    assert session.save()


def _new_conversation():
    """What /clear does to the session."""
    state.conversation_history.clear()
    session.clear()


# --- /rename ------------------------------------------------------------------------------

def test_a_name_is_saved_and_shown_in_the_list():
    _save("fix the login bug")
    assert "auth-fix" in session.rename("auth-fix")
    [entry] = session.conversations()
    assert entry["name"] == "auth-fix"
    assert entry["label"].startswith("auth-fix")
    assert json.loads(session.session_file().read_text(encoding="utf-8"))["name"] == "auth-fix"


def test_a_name_given_before_the_first_turn_is_saved_with_it():
    session.rename("early")
    assert not session.session_file().exists()
    _save("first question")
    assert session.conversations()[0]["name"] == "early"


def test_rename_without_a_name_shows_the_current_one():
    assert "not named" in session.rename("")
    session.rename("auth-fix")
    assert "auth-fix" in session.rename("")


@pytest.mark.parametrize("bad", ["x" * 61, "two\nlines", "tab\there", "--flag"])
def test_a_name_that_cannot_be_typed_back_is_refused(bad):
    _save("q")
    message = session.rename(bad)
    assert state.session_name is None
    assert "name" in message.lower()


def test_a_name_another_conversation_has_is_refused():
    _save("old")
    session.rename("auth-fix")
    _new_conversation()
    _save("new")
    assert "already" in session.rename("Auth-Fix")
    assert state.session_name is None


def test_clear_starts_unnamed_and_the_archive_keeps_its_name():
    _save("old")
    session.rename("auth-fix")
    _new_conversation()
    assert state.session_name is None
    assert [e["name"] for e in session.conversations()] == ["auth-fix"]


def test_restoring_a_conversation_restores_its_name():
    _save("old")
    session.rename("auth-fix")
    state.session_name = None
    session.restore()
    assert state.session_name == "auth-fix"


def test_resuming_an_unnamed_conversation_drops_the_name():
    _save("unnamed one")
    _new_conversation()
    _save("named one")
    session.rename("keep")
    unnamed = next(e for e in session.conversations() if e["name"] is None)
    session.resume(unnamed["path"])
    assert state.session_name is None


# --- pruning and lookup -------------------------------------------------------------------

def test_a_named_conversation_is_never_pruned():
    _save("the important one")
    session.rename("keep-me")
    _new_conversation()
    for i in range(config.SESSION_KEEP + 3):
        _save(f"conversation {i}")
        _new_conversation()
    entries = session.conversations()
    assert "keep-me" in [e["name"] for e in entries]
    assert sum(1 for e in entries if e["name"] is None) == config.SESSION_KEEP


def test_find_matches_a_name_whatever_its_case():
    _save("old")
    session.rename("Auth-Fix")
    _new_conversation()
    _save("other")
    found = session.find("auth-fix")
    assert found is not None and found["first"] == "old"
    assert session.find("nothing") is None


# --- --resume <name> ------------------------------------------------------------------------

def _main(argv, isatty=True):
    import llm_light.main as main_mod
    with patch.object(main_mod.sys, "argv", ["llm_light", *argv]), \
         patch.object(main_mod.sys.stdin, "isatty", return_value=isatty), \
         patch.object(main_mod, "_start_llmlingua_load"), \
         patch.object(main_mod, "start_chat") as start_chat, \
         patch.object(main_mod, "continue_turn") as continue_turn, \
         patch.object(main_mod, "one_shot") as one_shot:
        try:
            main_mod.main()
        except SystemExit as done:  # a -p turn exits with the turn's status
            if done.code != 0:
                raise
    return start_chat, continue_turn, one_shot


def _two_conversations():
    _save("the named one")
    session.rename("auth-fix")
    _new_conversation()
    _save("the latest one")


def _current_first_question():
    data = json.loads(session.session_file().read_text(encoding="utf-8"))
    return data["conversation_history"][0]["content"]


def test_resume_by_name_continues_it_without_asking():
    _two_conversations()
    start_chat, _, _ = _main(["--resume", "auth-fix"])
    start_chat.assert_called_once_with(auto_apply=False, resume=True, pick=False, load_compressor=ANY)
    assert _current_first_question() == "the named one"


def test_resume_by_name_answers_a_question_there():
    _two_conversations()
    _, continue_turn, one_shot = _main(["--resume", "auth-fix", "-p", "and then?"], isatty=False)
    continue_turn.assert_called_once()
    one_shot.assert_not_called()
    assert continue_turn.call_args.args[0] == "and then?"
    assert _current_first_question() == "the named one"


def test_resume_with_an_unknown_name_is_a_usage_error_naming_the_known_ones(capsys):
    _two_conversations()
    with pytest.raises(SystemExit) as raised:
        _main(["--resume", "nope"])
    assert raised.value.code == 2
    err = capsys.readouterr().err
    assert "nope" in err and "auth-fix" in err


def test_resume_without_a_name_still_asks():
    start_chat, _, _ = _main(["--resume"])
    start_chat.assert_called_once_with(auto_apply=False, resume=False, pick=True, load_compressor=ANY)


# --- the commands ---------------------------------------------------------------------------

def test_rename_is_a_listed_command():
    from llm_light import main
    assert "/rename" in {cmd.split()[0] for cmd, _ in main._COMMANDS}


def test_status_shows_the_name():
    state.session_name = "auth-fix"
    line = next(l for l in commands.status_lines() if l.startswith("Conversation"))
    assert "auth-fix" in line
