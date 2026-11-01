"""The assistant features modelled on Claude Code: permission modes (including
--dangerously-skip-permissions), /copy, !command, @file mentions, /model,
/export, /status, /diff, --version and -p."""

import io
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import llm_light.state as state
from llm_light import commands, config, permissions
from llm_light.tools import run_tool


# --- permission modes -------------------------------------------------------

def test_modes_parse_canonical_names_and_aliases():
    assert permissions.parse("default") == permissions.DEFAULT
    assert permissions.parse("acceptEdits") == permissions.ACCEPT_EDITS
    assert permissions.parse("accept-edits") == permissions.ACCEPT_EDITS
    assert permissions.parse("plan") == permissions.PLAN
    assert permissions.parse("yolo") == permissions.BYPASS
    assert permissions.parse("bypassPermissions") == permissions.BYPASS
    assert permissions.parse("nonsense") is None


def test_bypass_needs_the_startup_flag():
    # Claude Code only offers bypass when started with the flag; a typo in
    # /permissions must not switch every safeguard off.
    with pytest.raises(ValueError, match="dangerously-skip-permissions"):
        permissions.set_mode(permissions.BYPASS)
    assert permissions.mode() == permissions.DEFAULT
    state.bypass_allowed = True
    assert permissions.set_mode("yolo") == permissions.BYPASS
    assert permissions.bypass() and permissions.auto_edits()


def test_cycle_skips_bypass_unless_allowed():
    seen = [permissions.cycle() for _ in range(3)]
    assert seen == [permissions.ACCEPT_EDITS, permissions.PLAN, permissions.DEFAULT]
    state.bypass_allowed = True
    seen = [permissions.cycle() for _ in range(4)]
    assert seen == [permissions.ACCEPT_EDITS, permissions.PLAN, permissions.BYPASS, permissions.DEFAULT]


def test_question_note_is_empty_in_default_and_accept_edits():
    # Default-mode requests must stay byte-identical, so the cached prefix and
    # the benchmarks are unaffected.
    assert permissions.question_note(api_shell=True) == ""
    permissions.set_mode(permissions.ACCEPT_EDITS)
    assert permissions.question_note(api_shell=True) == ""
    permissions.set_mode(permissions.PLAN)
    assert "Plan mode" in permissions.question_note(api_shell=True)
    state.bypass_allowed = True
    permissions.set_mode(permissions.BYPASS)
    assert "any command" in permissions.question_note(api_shell=True)
    assert permissions.question_note(api_shell=False) == ""


def _edit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # edits must land inside the project root
    target = tmp_path / "a.txt"
    target.write_text("old\n", encoding="utf-8")
    return [{"path": "a.txt", "type": "new", "content": "new\n"}], target


@pytest.mark.parametrize("mode,applied", [
    (permissions.DEFAULT, False), (permissions.ACCEPT_EDITS, True), (permissions.BYPASS, True)])
def test_edits_apply_without_asking_in_accept_and_bypass(tmp_path, monkeypatch, mode, applied):
    from llm_light import editor
    state.bypass_allowed = True
    permissions.set_mode(mode)
    edits, target = _edit(tmp_path, monkeypatch)
    with patch.object(sys.stdin, "isatty", return_value=True), \
         patch.object(editor, "choose", return_value="no") as ask, \
         patch.object(editor.git_manager, "is_git_repo", return_value=False), \
         patch("builtins.print"):
        editor.apply_edits(edits, explanation="x")
    assert ask.called is (not applied)
    assert (target.read_text(encoding="utf-8") == "new\n") is applied


def test_shell_tool_runs_any_command_only_in_bypass(tmp_path):
    with patch.object(config, "ALLOWED_COMMANDS", []):
        out, is_error = run_tool("shell", {"command": "echo one && echo two"}, root=tmp_path)
        assert is_error, out  # chaining is refused outside bypass
        state.bypass_allowed = True
        permissions.set_mode(permissions.BYPASS)
        out, is_error = run_tool("shell", {"command": "echo one && echo two"}, root=tmp_path)
    assert not is_error, out
    assert "one" in out and "two" in out


def test_shell_tool_refuses_everything_in_plan_mode(tmp_path):
    permissions.set_mode(permissions.PLAN)
    with patch.object(config, "ALLOWED_COMMANDS", ["echo"]):
        out, is_error = run_tool("shell", {"command": "echo hi"}, root=tmp_path)
    assert is_error and "plan mode" in out.lower()


def test_failed_affected_tests_never_ask_to_revert_in_bypass(tmp_path):
    from llm_light import affected_tests
    state.bypass_allowed = True
    permissions.set_mode(permissions.BYPASS)
    with patch.object(config, "ALLOWED_COMMANDS", ["python -m pytest"]), \
         patch.object(affected_tests, "find_affected_tests", return_value=["tests/test_a.py"]), \
         patch("llm_light.tools.run_tool", return_value=("boom", True)), \
         patch.object(sys.stdin, "isatty", return_value=True), \
         patch("llm_light.ui.choose") as ask, \
         patch("llm_light.git_manager.undo_last_commit") as undo, \
         patch("builtins.print"):
        assert affected_tests.check_after_edit(["a.py"], root=tmp_path) is False
    ask.assert_not_called()
    undo.assert_not_called()


# --- chat() in plan mode -----------------------------------------------------

def _run_chat(reply, query="change it"):
    from llm_light import llm
    provider = MagicMock()
    provider.complete.return_value = (reply, None, True)
    provider.name = "claude"
    provider.has.return_value = False
    with patch.object(llm.providers, "active", return_value=provider), \
         patch.object(llm, "_finalize_route", return_value=("claude-sonnet-5-5", "low", 1024)), \
         patch.object(llm, "route_query", return_value=("claude-sonnet-5-5", "low", 1024)), \
         patch.object(llm, "retrieve", return_value=("", [])), \
         patch.object(llm, "record_retrieval"), \
         patch.object(llm, "_record_usage", return_value=False), \
         patch.object(llm, "_agent_oauth", return_value=False), \
         patch.object(llm, "apply_edits", return_value=[]) as apply, \
         patch("builtins.print"):
        llm.chat(query, use_index=False)
    request = provider.complete.call_args.args[0]
    question = request["messages"][-1]["content"][-1]["text"]
    return apply, question


_EDIT_REPLY = "Plan:\n```python:x.py\nprint(1)\n```\nDone."


def test_plan_mode_never_applies_edits_and_says_so_to_the_model():
    permissions.set_mode(permissions.PLAN)
    apply, question = _run_chat(_EDIT_REPLY)
    apply.assert_not_called()
    assert "Plan mode" in question and question.endswith("change it")
    # The reply is kept whole: the proposed blocks are the plan.
    assert state.conversation_history[-1]["content"] == _EDIT_REPLY
    assert state.conversation_history[-2]["content"] == "change it"


def test_default_mode_question_is_unchanged():
    apply, question = _run_chat(_EDIT_REPLY)
    assert question == "Question:\nchange it"
    assert apply.call_count == 2  # the lint check, then the write


def test_chat_attaches_context_after_the_question_and_records_the_reply():
    apply, question = _run_chat("an answer", query="explain")
    assert state.replies[-1] == "an answer"
    from llm_light import llm
    provider = MagicMock()
    provider.complete.return_value = ("ok", None, True)
    provider.has.return_value = False
    with patch.object(llm.providers, "active", return_value=provider), \
         patch.object(llm, "_finalize_route", return_value=("m", "low", 1024)), \
         patch.object(llm, "route_query", return_value=("m", "low", 1024)) as route, \
         patch.object(llm, "retrieve", return_value=("", [])), \
         patch.object(llm, "record_retrieval"), \
         patch.object(llm, "_record_usage", return_value=False), \
         patch.object(llm, "_agent_oauth", return_value=False), \
         patch("builtins.print"):
        llm.chat("what is in it", use_index=False, context="FILE BODY")
    # Routing sees the typed question only: an attached file must not push a
    # short question to the top tier.
    assert route.call_args.args[0] == "what is in it"
    text = provider.complete.call_args.args[0]["messages"][-1]["content"][-1]["text"]
    assert text == "Question:\nwhat is in it\n\nFILE BODY"
    assert state.conversation_history[-2]["content"] == "what is in it\n\nFILE BODY"


# --- /copy -------------------------------------------------------------------

def test_copy_takes_the_latest_reply_or_the_nth():
    state.replies[:] = ["first", "second"]
    with patch.object(commands, "copy_text", return_value="copied") as copy:
        assert commands.copy_reply("") == "copied"
        copy.assert_called_with("second")
        commands.copy_reply("2")
        copy.assert_called_with("first")
    assert "only 2" in commands.copy_reply("3")
    assert "Usage" in commands.copy_reply("x")


def test_copy_falls_back_to_the_saved_conversation():
    # After --continue there are no replies from this run yet.
    state.conversation_history[:] = [{"role": "user", "content": "q"},
                                     {"role": "assistant", "content": [{"type": "text", "text": "a"}]}]
    with patch.object(commands, "copy_text", return_value="copied") as copy:
        commands.copy_reply("")
    copy.assert_called_with("a")


def test_copy_with_nothing_to_copy():
    assert "Nothing to copy" in commands.copy_reply("")


@pytest.mark.parametrize("platform,which,expected", [
    ("win32", {}, ["clip"]),
    ("darwin", {"pbcopy": "/usr/bin/pbcopy"}, ["pbcopy"]),
    ("linux", {"wl-copy": "/usr/bin/wl-copy"}, ["wl-copy"]),
    ("linux", {"xclip": "/usr/bin/xclip"}, ["xclip", "-selection", "clipboard"]),
    ("linux", {"xsel": "/usr/bin/xsel"}, ["xsel", "--clipboard", "--input"]),
    ("linux", {}, None),
])
def test_clipboard_command_per_platform(platform, which, expected):
    with patch.object(commands.sys, "platform", platform), \
         patch.object(commands.shutil, "which", side_effect=lambda n: which.get(n)):
        assert commands._clipboard_command() == expected


def test_copy_text_pipes_utf16_to_clip_on_windows():
    # clip.exe reads UTF-16 as Unicode; UTF-8 would mangle anything non-ASCII.
    # With a BOM ("utf-16") the paste starts with U+FEFF: measured on
    # Windows 11, Get-Clipboard returned 18 characters for a 17-character text.
    with patch.object(commands.sys, "platform", "win32"), \
         patch.object(commands.subprocess, "run") as run:
        run.return_value.returncode = 0
        msg = commands.copy_text("héllo ✔")
    assert run.call_args.kwargs["input"] == "héllo ✔".encode("utf-16-le")
    assert "Copied 7 characters" in msg


def test_copy_text_falls_back_to_osc52_without_a_clipboard_tool():
    out = io.StringIO()
    with patch.object(commands, "_clipboard_command", return_value=None), \
         patch.object(commands.sys, "stdout", out):
        msg = commands.copy_text("hi")
    assert "\x1b]52;c;aGk=\x07" in out.getvalue()
    assert "OSC 52" in msg


# --- !command ----------------------------------------------------------------

def test_bang_command_runs_and_queues_output_for_the_next_message():
    with patch("llm_light.executor._run_command", return_value="$ ls\nfile.py\nExit code: 0") as run:
        commands.run_bang("ls")
    run.assert_called_once_with("ls")
    context = commands.take_pending()
    assert "ls" in context and "file.py" in context
    assert commands.take_pending() == ""  # sent once


def test_bang_without_a_command_is_usage():
    assert "Usage" in commands.run_bang("  ")


# --- @file mentions ------------------------------------------------------------

def test_mentions_attach_files_inside_the_project(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    context, attached, missing = commands.expand_mentions(
        "explain @src/a.py and mail me@example.com about @Override", root=tmp_path)
    assert attached == ["src/a.py"]
    assert "x = 1" in context and "src/a.py" in context
    assert missing == []  # @Override is not a path-like token; me@ is an email


def test_mentions_reject_paths_outside_the_project(tmp_path):
    outside = Path(tempfile.mkdtemp()) / "secret.txt"
    outside.write_text("s3cret", encoding="utf-8")
    context, attached, missing = commands.expand_mentions(f"read @{outside} and @../x.txt", root=tmp_path)
    assert attached == [] and "s3cret" not in context
    assert len(missing) == 2


def test_mentions_cap_large_files(tmp_path):
    (tmp_path / "big.txt").write_text("y" * (commands.MENTION_MAX_CHARS + 500), encoding="utf-8")
    context, attached, _ = commands.expand_mentions("@big.txt", root=tmp_path)
    assert attached == ["big.txt"]
    assert "truncated" in context
    assert len(context) < commands.MENTION_MAX_CHARS + 500


def test_mention_completion_lists_project_paths(tmp_path, monkeypatch):
    from prompt_toolkit.document import Document
    import llm_light.main as m
    (tmp_path / "alpha.py").write_text("", encoding="utf-8")
    (tmp_path / "beta").mkdir()
    monkeypatch.chdir(tmp_path)
    names = [c.text for c in m._SlashCompleter().get_completions(Document("look at @al"), None)]
    assert names == ["@alpha.py"]
    names = [c.text for c in m._SlashCompleter().get_completions(Document("@b"), None)]
    assert names == ["@beta/"]


# --- /model --------------------------------------------------------------------

def _claude_like():
    provider = MagicMock()
    provider.name = "claude"
    provider.has.return_value = True
    return provider


def test_model_command_pins_and_unpins():
    with patch.object(commands.providers, "active", return_value=_claude_like()):
        assert "opus-5-5" in commands.model_command("opus")
        assert state.pinned_model == config.MODEL_OPUS
        assert "pinned" in commands.model_command("")
        assert "Unknown model" in commands.model_command("gpt")
        assert "automatic" in commands.model_command("auto")
    assert state.pinned_model is None


def test_model_command_unavailable_without_top_tier():
    provider = MagicMock()
    provider.name = "gemini"
    provider.has.return_value = False
    with patch.object(commands.providers, "active", return_value=provider):
        assert "unavailable" in commands.model_command("opus")
    assert state.pinned_model is None


def test_pinned_model_overrides_routing_and_skips_the_top_tier_prompt():
    from llm_light import llm
    state.pinned_model = config.MODEL_HAIKU
    with patch.object(llm.providers, "active", return_value=_claude_like()), \
         patch.object(llm, "_top_tier_model") as ask, \
         patch("builtins.print"):
        model, effort, _ = llm._finalize_route(config.MODEL_OPUS, "high", 8000)
    assert model == config.MODEL_HAIKU and effort == "high"
    ask.assert_not_called()


# --- /export, /status, /diff, version -------------------------------------------

def test_export_writes_the_conversation(tmp_path):
    state.conversation_history[:] = [
        {"role": "user", "content": "what is x?"},
        {"role": "assistant", "content": [{"type": "text", "text": "x is 1"}]}]
    target = tmp_path / "talk.txt"
    msg = commands.export_conversation(str(target))
    text = target.read_text(encoding="utf-8")
    assert "what is x?" in text and "x is 1" in text
    assert "2 messages" in msg


def test_export_default_name_is_not_indexed(tmp_path, monkeypatch):
    # A .md file would land in the skeleton and change the cached prefix.
    monkeypatch.chdir(tmp_path)
    state.conversation_history[:] = [{"role": "user", "content": "q"}]
    commands.export_conversation("")
    (written,) = list(tmp_path.iterdir())
    assert written.suffix == ".txt"
    assert written.suffix not in config.INDEXABLE_EXTENSIONS


def test_export_with_empty_conversation():
    assert "Nothing to export" in commands.export_conversation("")


def test_status_lists_mode_model_and_shell():
    state.pinned_model = config.MODEL_SONNET
    permissions.set_mode(permissions.ACCEPT_EDITS)
    with patch.object(commands.providers, "active", return_value=_claude_like()), \
         patch.object(config, "ALLOWED_COMMANDS", ["python -m pytest"]):
        text = "\n".join(commands.status_lines())
        assert commands.version() in text
        assert "acceptEdits" in text and "sonnet-5-5" in text and "python -m pytest" in text
        permissions.set_mode(permissions.PLAN)
        text = "\n".join(commands.status_lines())
    # Plan mode runs no command, whatever the allowlist says.
    assert "off (plan mode)" in text and "python -m pytest" not in text


def test_diff_reports_a_clean_tree():
    with patch.object(commands.git_manager, "is_git_repo", return_value=True), \
         patch.object(commands.git_manager, "_run_git", return_value=("", 0)):
        assert commands.diff_command() == "No uncommitted changes."


def test_diff_shows_each_changed_file():
    # --relative: paths relative to the directory llm_light runs in, which
    # may be below the repository root.
    calls = {("diff", "HEAD", "--name-only", "--relative"): ("a.py", 0),
             ("ls-files", "--others", "--exclude-standard"): ("new.py", 0),
             ("diff", "HEAD", "--relative", "--", "a.py"): ("@@ -1 +1 @@\n-a\n+b", 0)}
    with patch.object(commands.git_manager, "is_git_repo", return_value=True), \
         patch.object(commands.git_manager, "_run_git", side_effect=lambda *a: calls[a]), \
         patch("llm_light.ui.show_unified_diff") as show:
        msg = commands.diff_command()
    show.assert_called_once()
    assert show.call_args.args[0] == "a.py"
    assert "1 changed file" in msg and "new.py" in msg


def test_version_is_the_project_version():
    import tomllib
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    expected = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    assert commands.version() == expected


# --- main(): flags ----------------------------------------------------------------

def _main(monkeypatch, argv, tty=False):
    from llm_light import main as main_mod
    fake = MagicMock()
    fake.has.return_value = True
    monkeypatch.setattr(sys, "argv", ["llm_light"] + argv)
    monkeypatch.setattr(main_mod.providers, "select", MagicMock(return_value=fake))
    one_shot, start_chat = MagicMock(), MagicMock()
    monkeypatch.setattr(main_mod, "one_shot", one_shot)
    monkeypatch.setattr(main_mod, "start_chat", start_chat)
    monkeypatch.setattr(main_mod, "_start_llmlingua_load", lambda: None)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: tty)
    main_mod.main()
    return one_shot, start_chat


@pytest.mark.parametrize("flag", ["--dangerously-skip-permissions", "--yolo"])
def test_dangerous_flag_starts_in_bypass(monkeypatch, flag, capsys):
    _main(monkeypatch, [flag, "q"])
    assert permissions.mode() == permissions.BYPASS and state.bypass_allowed
    assert "bypass" in capsys.readouterr().err.lower()


def test_yes_flag_starts_in_accept_edits(monkeypatch):
    _main(monkeypatch, ["-y", "q"])
    assert permissions.mode() == permissions.ACCEPT_EDITS
    assert not state.bypass_allowed


def test_permission_mode_flag(monkeypatch):
    _main(monkeypatch, ["--permission-mode", "plan", "q"])
    assert permissions.mode() == permissions.PLAN


def test_allow_dangerously_skip_permissions_offers_bypass_without_starting_in_it(monkeypatch):
    _main(monkeypatch, ["--allow-dangerously-skip-permissions", "q"])
    assert permissions.mode() == permissions.DEFAULT and state.bypass_allowed


def test_model_flag_pins_the_model(monkeypatch):
    _main(monkeypatch, ["--model", "haiku", "q"])
    assert state.pinned_model == config.MODEL_HAIKU


def test_bad_model_flag_exits(monkeypatch):
    with pytest.raises(SystemExit):
        _main(monkeypatch, ["--model", "gpt-9", "q"])


def test_version_flag(monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        _main(monkeypatch, ["--version"])
    assert exc.value.code == 0
    assert commands.version() in capsys.readouterr().out


def test_print_flag_runs_one_shot(monkeypatch):
    one_shot, start_chat = _main(monkeypatch, ["-p", "what is this?"], tty=True)
    one_shot.assert_called_once()
    assert one_shot.call_args.args[0] == "what is this?"
    start_chat.assert_not_called()


def test_print_flag_without_a_question_on_a_terminal_errors(monkeypatch):
    with pytest.raises(SystemExit):
        _main(monkeypatch, ["-p"], tty=True)


def test_one_shot_mentions_are_attached(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    one_shot, _ = _main(monkeypatch, ["explain @a.py"])
    assert one_shot.call_args.args[0] == "explain @a.py"
    assert "x = 1" in one_shot.call_args.kwargs["context"]


# --- the interactive loop ------------------------------------------------------

def _loop(monkeypatch, inputs, **chat_kwargs):
    import llm_light as cl
    feed = iter(inputs)
    chat = MagicMock()
    with patch.object(cl, "full_refresh"), \
         patch("llm_light.Observer", return_value=MagicMock()), \
         patch("llm_light.SourceHandler", return_value=MagicMock()), \
         patch("threading.Thread", return_value=MagicMock()), \
         patch.object(cl, "_PROMPTTK_AVAILABLE", False), \
         patch.object(cl, "print_session_summary"), \
         patch.object(cl, "chat", chat), \
         patch.object(cl.main.session, "save"), \
         patch("builtins.input", side_effect=lambda _p=">": next(feed)), \
         patch("builtins.print"):
        try:
            cl.start_chat(**chat_kwargs)
        finally:
            # start_chat() sets stop_event on the way out; left set, it stops
            # the hybrid index's background pass in tests that run later.
            state.stop_event.clear()
    return chat


def test_loop_sends_bang_output_with_the_next_question(monkeypatch):
    with patch("llm_light.executor._run_command", return_value="OUT"):
        chat = _loop(monkeypatch, ["!echo hi", "why?", "exit"])
    chat.assert_called_once()
    assert chat.call_args.args[0] == "why?"
    assert "OUT" in chat.call_args.kwargs["context"]


def test_loop_permissions_command_switches_mode(monkeypatch):
    _loop(monkeypatch, ["/permissions plan", "exit"])
    assert permissions.mode() == permissions.PLAN


def test_loop_yes_flag_becomes_a_mode_that_shift_tab_can_leave(monkeypatch):
    # -y used to be a fixed auto_apply argument; now it is acceptEdits, so
    # cycling back to default asks again.
    chat = _loop(monkeypatch, ["/permissions default", "q", "exit"], auto_apply=True)
    assert chat.call_args.kwargs["auto_apply"] is False


@pytest.mark.parametrize("cmd", ["/exit", "/quit"])
def test_loop_exit_commands(monkeypatch, cmd):
    chat = _loop(monkeypatch, [cmd, "never reached"])
    chat.assert_not_called()


def test_loop_copy_and_unknown_commands_do_not_reach_chat(monkeypatch):
    with patch.object(commands, "copy_text", return_value="ok"):
        chat = _loop(monkeypatch, ["/copy", "/status", "/model", "/bogus", "exit"])
    chat.assert_not_called()


# --- toolbar --------------------------------------------------------------------

def test_toolbar_shows_non_default_modes():
    import llm_light.main as m
    state.indexing_done.set()
    assert "plan" not in m._status_text(200)
    permissions.set_mode(permissions.PLAN)
    assert "plan mode" in m._status_text(200)
    state.bypass_allowed = True
    permissions.set_mode(permissions.BYPASS)
    narrow = m._status_text(40)
    assert "bypass" in narrow  # never dropped to fit
