"""The prompt's editing keys, completion, and `!!command`, through a real PromptSession."""

import os
import sys
import threading
from unittest.mock import patch

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

import llm_light.state as state
from llm_light import commands
from llm_light import main as m


def _prompt(keys, history=None, **kwargs):
    with create_pipe_input() as inp:
        inp.send_text(keys)
        return PromptSession(input=inp, output=DummyOutput(), history=history or InMemoryHistory(),
                             key_bindings=m._prompt_bindings(), **kwargs).prompt()


# --- new lines ----------------------------------------------------------------------

def test_backslash_enter_adds_a_line_without_the_backslash():
    assert _prompt("one\\\rtwo\r") == "one\ntwo"


def test_a_backslash_elsewhere_stays():
    assert _prompt("C:\\dir\\file\r") == "C:\\dir\\file"


def test_enter_after_a_backslash_earlier_in_the_line_still_sends():
    assert _prompt("a\\b\r") == "a\\b"


# --- ctrl+g -------------------------------------------------------------------------

def test_ctrl_g_opens_the_editor():
    with patch.object(Buffer, "open_in_editor") as editor:
        assert _prompt("draft\x07\r") == "draft"
    editor.assert_called_once()


def test_an_editor_is_chosen_on_windows_when_none_is_set(monkeypatch):
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.setenv("SystemRoot", "D:\\Win")
    with patch.object(commands, "_on_windows", return_value=True):
        commands.ensure_editor()
    editor = os.environ["EDITOR"]
    monkeypatch.delenv("EDITOR")
    assert editor == "D:/Win/System32/notepad.exe"  # prompt_toolkit runs it through shlex


def test_a_set_editor_is_left_alone(monkeypatch):
    monkeypatch.setenv("EDITOR", "my-editor")
    commands.ensure_editor()
    assert os.environ["EDITOR"] == "my-editor"


# --- keys prompt_toolkit should already give us; these fail if a binding shadows one -----

def test_ctrl_r_searches_the_history():
    history = InMemoryHistory()
    history.append_string("deploy the thing")
    history.append_string("something else")
    with create_pipe_input() as inp:
        # After the prompt is up: the session loads its history in the background, so
        # keys sent any earlier search an empty one.
        timer = threading.Timer(0.3, lambda: inp.send_text("\x12deploy\r\r"))
        timer.start()
        try:
            answer = PromptSession(input=inp, output=DummyOutput(), history=history,
                                   key_bindings=m._prompt_bindings()).prompt()
        finally:
            timer.join()
    assert answer == "deploy the thing"


def test_ctrl_l_keeps_what_is_typed():
    assert _prompt("typed\x0c\r") == "typed"


def test_ctrl_a_and_ctrl_e_move_the_cursor():
    assert _prompt("bc\x01a\x05d\r") == "abcd"


def test_ctrl_w_deletes_a_word():
    assert _prompt("keep drop\x17\r") == "keep "


# --- /vim ---------------------------------------------------------------------------

def test_vim_command_toggles_and_says_so():
    state.vim_mode = False
    assert "on" in commands.vim_command("")
    assert state.vim_mode is True
    assert "off" in commands.vim_command("")
    assert state.vim_mode is False


def test_vi_mode_edits_with_vi_keys():
    # esc to normal mode, 0 to line start, x to delete a character, enter to send.
    assert _prompt("xhello\x1b0x\r", vi_mode=True) == "hello"


# --- completion ---------------------------------------------------------------------

def _complete(text):
    return [c.text for c in m._SlashCompleter().get_completions(Document(text), None)]


def test_model_names_complete():
    assert _complete("/model o") == ["opus"]
    assert "auto" in _complete("/model ")


def test_permission_modes_complete():
    assert _complete("/permissions pl") == ["plan"]


def test_other_arguments_are_not_completed():
    assert _complete("/run py") == []


def _project(tmp_path):
    for name in ("src/deep/order_service.py", "src/other.py", "README.md", "node_modules/x/order_mod.js"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
    commands._project_files.cache_clear()
    return tmp_path


def test_mention_finds_a_file_anywhere_by_name(tmp_path):
    assert "@src/deep/order_service.py" in commands.mention_completions("@order", root=_project(tmp_path))


def test_mention_search_skips_build_directories(tmp_path):
    found = commands.mention_completions("@order", root=_project(tmp_path))
    assert not any("node_modules" in path for path in found)


def test_mention_prefers_the_name_over_the_path(tmp_path):
    root = _project(tmp_path)
    (root / "order").mkdir()
    (root / "order" / "zzz.py").write_text("x", encoding="utf-8")
    commands._project_files.cache_clear()
    found = commands.mention_completions("@order", root=root)
    assert found.index("@src/deep/order_service.py") < found.index("@order/zzz.py")


def test_mention_matches_initials_in_order(tmp_path):
    assert "@src/deep/order_service.py" in commands.mention_completions("@ordsrv", root=_project(tmp_path))


def test_mention_with_a_slash_still_lists_that_folder(tmp_path):
    root = _project(tmp_path)
    assert commands.mention_completions("@src/", root=root) == ["@src/deep/", "@src/other.py"]


def test_mention_search_is_capped(tmp_path):
    for i in range(80):
        (tmp_path / f"file{i}.py").write_text("x", encoding="utf-8")
    commands._project_files.cache_clear()
    assert len(commands.mention_completions("@file", root=tmp_path)) == 50


# --- !!command ----------------------------------------------------------------------

def test_double_bang_reports_the_exit_code(capfd):
    line = commands.run_interactive(f'"{sys.executable}" -c "raise SystemExit(3)"')
    assert "3" in line and "nothing" in line.lower()


def test_double_bang_sends_nothing_to_the_model():
    state.pending_context.clear()
    commands.run_interactive(f'"{sys.executable}" -c "pass"')
    assert state.pending_context == []


def test_double_bang_inherits_the_terminal():
    """No pipes and no closed stdin: the whole point is that the program owns the terminal."""
    with patch.object(commands.subprocess, "call", return_value=0) as call:
        commands.run_interactive("vim x")
    kwargs = call.call_args.kwargs
    assert not {"stdin", "stdout", "stderr", "start_new_session"} & kwargs.keys()


def test_double_bang_without_a_command_shows_usage():
    assert "Usage" in commands.run_interactive("  ")


def test_ctrl_c_during_double_bang_returns_to_the_prompt():
    with patch.object(commands.subprocess, "call", side_effect=KeyboardInterrupt):
        assert "Stopped" in commands.run_interactive("vim x")


def test_a_command_that_cannot_start_is_reported():
    with patch.object(commands.subprocess, "call", side_effect=OSError("no such file")):
        assert "no such file" in commands.run_interactive("vim x")


@pytest.fixture(autouse=True)
def _reset_vim():
    yield
    state.vim_mode = False
