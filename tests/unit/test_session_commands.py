"""/init, /review, /doctor, /context, /memory, /effort and /compact <focus>."""

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import llm_light.state as state
from llm_light import commands, config, doctor, llm
from llm_light import main as m
from llm_light.providers import claude, codex, gemini


@pytest.fixture(autouse=True)
def _clean():
    state.conversation_history.clear()
    state.pending_context.clear()
    state.pinned_effort = None
    yield
    state.conversation_history.clear()
    state.pending_context.clear()
    state.pinned_effort = None


def _git(root, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root, check=True,
                   capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "first")
    monkeypatch.chdir(tmp_path)
    return tmp_path


# --- /init --------------------------------------------------------------------------

def test_init_asks_for_the_active_providers_file(repo):
    with patch.object(commands.providers, "active", return_value=claude.PROVIDER):
        assert "CLAUDE.md" in commands.init_prompt()
    with patch.object(commands.providers, "active", return_value=codex.PROVIDER):
        assert "AGENTS.md" in commands.init_prompt()


def test_init_improves_a_file_that_exists_instead_of_replacing_it(repo):
    (repo / "CLAUDE.md").write_text("# mine\n", encoding="utf-8")
    with patch.object(commands.providers, "active", return_value=claude.PROVIDER):
        prompt = commands.init_prompt()
    assert "already exists" in prompt and "improve" in prompt.lower()


# --- /review ------------------------------------------------------------------------

def test_review_without_changes_sends_nothing(repo):
    prompt, diff, line = commands.review_prompt("")
    assert prompt is None and diff == "" and "No changes" in line


def test_review_sends_the_uncommitted_diff(repo):
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    prompt, diff, line = commands.review_prompt("")
    assert "+x = 2" in diff and "Do not edit" in prompt
    assert "1 changed file" in line


def test_review_keeps_the_diff_out_of_the_question(repo):
    # Routing and retrieval read the question only (#320): a diff in it scored its file
    # names, its length and words like "fix" or "security" toward the top tier, and
    # retrieval embedded raw diff text.
    (repo / "a.py").write_text("x = 2  # fix the security issue in a.py\n", encoding="utf-8")
    prompt, diff, _ = commands.review_prompt("")
    assert "+x = 2" not in prompt and "a.py" not in prompt
    assert "+x = 2" in diff


def test_review_against_a_ref_includes_commits(repo):
    (repo / "a.py").write_text("x = 3\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "second")
    _, diff, _ = commands.review_prompt("HEAD~1")
    assert "+x = 3" in diff


def test_review_names_untracked_files(repo):
    (repo / "new.py").write_text("y = 1\n", encoding="utf-8")
    _, diff, _ = commands.review_prompt("")
    assert "new.py" in diff


@pytest.mark.parametrize("bad", ["--output=/tmp/x", "-p", "a b", "x;y"])
def test_review_refuses_what_is_not_a_ref(repo, bad):
    prompt, _, line = commands.review_prompt(bad)
    assert prompt is None and "ref" in line


def test_review_reports_an_unknown_ref(repo):
    prompt, _, line = commands.review_prompt("no-such-branch")
    assert prompt is None and "no-such-branch" in line


def test_review_outside_git_says_so(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    prompt, _, line = commands.review_prompt("")
    assert prompt is None and "git" in line


def test_a_huge_diff_is_cut_and_says_so(repo):
    (repo / "a.py").write_text("x = 1\n" + "y = 2\n" * 40_000, encoding="utf-8")
    _, diff, _ = commands.review_prompt("")
    assert len(diff) < commands.MENTION_MAX_CHARS + 1_000 and "truncated" in diff


def test_a_big_diff_does_not_route_review_to_the_top_tier(repo):
    # A real-looking diff: many files, long, full of the words the router scores.
    for i in range(30):
        (repo / f"service_{i}.py").write_text(
            "def fix_security_performance_bug():\n    # refactor, update, add tests, migrate\n"
            "    return architecture_redesign()\n" * 20, encoding="utf-8")
    _git(repo, "add", ".")
    prompt, diff, _ = commands.review_prompt("HEAD")
    assert "service_0.py" in diff
    assert llm.route_query(prompt)[0] != config.MODEL_OPUS


# --- /doctor ------------------------------------------------------------------------

def test_doctor_lists_the_environment_with_no_secret(repo):
    with patch.object(config, "API_KEY", "sk-ant-api03-SECRET"), \
            patch.object(config, "AUTH_TOKEN", "sk-ant-api03-SECRET"), \
            patch.object(config, "AUTH_MODE", "API_KEY"), \
            patch.object(config, "API_KEY_SOURCE", "Environment"):
        report = "\n".join(line for _, line in doctor.checks())
    assert "SECRET" not in report
    assert "Python" in report and "git" in report.lower() and "API key" in report


def test_doctor_flags_old_python():
    with patch.object(doctor.sys, "version_info", (3, 11, 0)):
        status, line = doctor._python()
    assert status == doctor.PROBLEM and "3.12" in line


def test_doctor_flags_missing_credentials():
    with patch.object(commands.providers, "active", return_value=claude.PROVIDER), \
            patch.object(config, "AUTH_MODE", "OAUTH"), patch.object(config, "API_KEY", ""), \
            patch.object(config, "AUTH_TOKEN", ""), patch.object(claude, "_find_cli", return_value=None):
        statuses = claude.PROVIDER.diagnose()
    assert doctor.PROBLEM in [status for status, _ in statuses]


def test_doctor_starts_no_program_for_any_provider(repo):
    """What the docs promise, and what makes it safe to run in a repo you do not trust."""
    with patch("subprocess.run", side_effect=AssertionError("ran something")), \
            patch("subprocess.Popen", side_effect=AssertionError("ran something")):
        for provider in (claude.PROVIDER, codex.PROVIDER, gemini.PROVIDER):
            assert provider.diagnose()
            with patch.object(commands.providers, "active", return_value=provider):
                assert doctor.checks()


def test_doctor_finds_the_repository_from_a_subfolder_without_git(repo, monkeypatch):
    (repo / "sub").mkdir()
    monkeypatch.chdir(repo / "sub")
    assert doctor._in_repository() is True


def test_doctor_says_when_there_is_no_repository(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch.object(doctor.Path, "exists", return_value=False), \
            patch.object(doctor.shutil, "which", return_value="/usr/bin/git"):
        status, line = doctor._git()
    assert status == doctor.NOTE and "not a repository" in line


def test_doctor_exits_non_zero_on_a_problem():
    with patch.object(doctor, "checks", return_value=[(doctor.OK, "fine"), (doctor.PROBLEM, "broken")]):
        assert doctor.run() == 1
    with patch.object(doctor, "checks", return_value=[(doctor.OK, "fine"), (doctor.NOTE, "fyi")]):
        assert doctor.run() == 0


def test_doctor_flag_works_without_credentials(monkeypatch, capsys):
    """Broken auth is the most likely reason to run it, so it comes before require_auth."""
    provider = MagicMock()
    provider.has.return_value = True
    provider.diagnose.return_value = [(doctor.PROBLEM, "not signed in")]
    provider.require_auth.side_effect = AssertionError("require_auth ran first")
    monkeypatch.setattr(sys, "argv", ["llm_light", "--doctor"])
    monkeypatch.setattr(m.providers, "select", MagicMock(return_value=provider))
    monkeypatch.setattr(m.providers, "active", MagicMock(return_value=provider))
    monkeypatch.setattr(m, "_start_llmlingua_load", lambda: None)
    with pytest.raises(SystemExit) as stopped:
        m.main()
    assert stopped.value.code == 1
    assert "not signed in" in capsys.readouterr().out


# --- /context -----------------------------------------------------------------------

def test_context_counts_each_part():
    state.conversation_history[:] = [{"role": "user", "content": "q" * 4000},
                                     {"role": "assistant", "content": "a" * 4000}]
    state.pending_context.append("p" * 400)
    with patch.object(state, "skeleton_context", "s" * 8000):
        rows = dict(doctor.context_rows())
    assert rows["Conversation"] == 2000
    assert rows["Skeleton"] >= 2000
    assert rows["Queued output"] == 100


def test_context_counts_your_instructions_and_the_appended_prompt(monkeypatch):
    # Both go with every turn (llm._build_system_blocks) but /context left them out (#331).
    from llm_light import instructions
    monkeypatch.setattr(instructions, "text", lambda: "i" * 400)
    monkeypatch.setattr(state, "extra_system", "x" * 800)
    rows = dict(doctor.context_rows())
    assert rows["Your instructions"] == 100
    assert rows["--append-system-prompt"] == 200


def test_context_leaves_out_empty_instructions(monkeypatch):
    from llm_light import instructions
    monkeypatch.setattr(instructions, "text", lambda: "")
    monkeypatch.setattr(state, "extra_system", "")
    rows = dict(doctor.context_rows())
    assert "Your instructions" not in rows and "--append-system-prompt" not in rows


def test_context_counts_the_agent_prompt_when_the_agent_gets_it(monkeypatch):
    # With commands allowed, the subscription agent gets AGENT_SYSTEM_PROMPT instead.
    from llm_light import permissions
    monkeypatch.setattr(doctor, "_agent_adds_its_own", lambda: True)
    monkeypatch.setattr(config, "ALLOWED_COMMANDS", ["python -m pytest"])
    permissions.set_mode(permissions.DEFAULT)
    assert dict(doctor.context_rows())["System prompt"] == len(config.AGENT_SYSTEM_PROMPT) // 4
    monkeypatch.setattr(config, "ALLOWED_COMMANDS", [])
    assert dict(doctor.context_rows())["System prompt"] == len(config.SYSTEM_PROMPT) // 4


def test_context_names_what_the_last_question_retrieved(capsys, monkeypatch):
    monkeypatch.setattr(state, "last_retrieval", {"hits": [("a.py::f", 1.0)] * 5, "retrieved_tokens": 1234})
    doctor.print_context()
    out = capsys.readouterr().out
    assert "1,234" in out and "5 chunks" in out


def test_context_says_which_parts_repeat_every_turn(capsys):
    doctor.print_context()
    out = capsys.readouterr().out
    assert "every turn" in out and "conversation grows" in out


def test_a_turn_records_how_much_it_retrieved(monkeypatch):
    usage = MagicMock(input_tokens=1, output_tokens=1, cache_creation_input_tokens=0,
                      cache_read_input_tokens=0)
    with patch.object(llm, "retrieve", return_value=("c" * 4000, [("a.py::f", 1.0)])), \
            patch("llm_light.providers.claude._make_streaming_api_call",
                  return_value=("answer", usage, False)), \
            patch.object(llm, "parse_edit_blocks", return_value=[]), \
            patch.object(llm, "_print_reply"), patch.object(llm, "print_stats"), \
            patch("llm_light.config.LLMLINGUA_ENABLED", False):
        llm.chat("where is the retry loop?")
    assert state.last_retrieval["retrieved_tokens"] == 1000


def test_context_only_counts_tools_where_llm_light_sends_them():
    with patch.object(doctor, "_sends_own_tools", return_value=True):
        assert dict(doctor.context_rows())["Tool definitions"] > 0
    with patch.object(doctor, "_sends_own_tools", return_value=False):
        assert "Tool definitions" not in dict(doctor.context_rows())


def test_context_says_it_is_an_estimate_and_what_it_leaves_out(capsys):
    with patch.object(doctor, "_agent_adds_its_own", return_value=True):
        doctor.print_context()
    out = capsys.readouterr().out
    assert "estimate" in out.lower() and "agent" in out.lower()


# --- /memory ------------------------------------------------------------------------

def test_memory_opens_the_file_that_exists(repo):
    (repo / "AGENTS.md").write_text("# a\n", encoding="utf-8")
    with patch.object(commands.subprocess, "call", return_value=0) as call, \
            patch.object(commands, "editor_argv", return_value=["my-editor"]):
        line = commands.memory_command(choose=lambda *_: "AGENTS.md")
    assert call.call_args.args[0][0] == "my-editor"
    assert call.call_args.args[0][-1].endswith("AGENTS.md")
    assert "AGENTS.md" in line


def test_memory_creates_the_providers_file_when_there_is_none(repo):
    with patch.object(commands.providers, "active", return_value=claude.PROVIDER), \
            patch.object(commands.subprocess, "call", return_value=0), \
            patch.object(commands, "editor_argv", return_value=["my-editor"]):
        commands.memory_command(choose=lambda *_: None)
    assert (repo / "CLAUDE.md").exists()


def test_memory_cancelled_opens_nothing(repo):
    (repo / "CLAUDE.md").write_text("x", encoding="utf-8")
    (repo / "AGENTS.md").write_text("y", encoding="utf-8")
    with patch.object(commands.subprocess, "call") as call:
        assert "Cancelled" in commands.memory_command(choose=lambda *_: None)
    call.assert_not_called()


def test_memory_without_an_editor_says_which_file(repo):
    (repo / "CLAUDE.md").write_text("x", encoding="utf-8")
    with patch.object(commands, "editor_argv", return_value=None):
        line = commands.memory_command(choose=lambda *_: "CLAUDE.md")
    assert "CLAUDE.md" in line and "EDITOR" in line


# --- /effort ------------------------------------------------------------------------

def test_effort_pin_and_unpin():
    with patch.object(commands.providers, "active", return_value=claude.PROVIDER):
        assert "high" in commands.effort_command("high")
        assert state.pinned_effort == "high"
        assert "high" in commands.effort_command("")
        assert "routed" in commands.effort_command("auto")
    assert state.pinned_effort is None


def test_effort_rejects_what_it_does_not_know():
    with patch.object(commands.providers, "active", return_value=claude.PROVIDER):
        assert "Unknown" in commands.effort_command("ultra")
    assert state.pinned_effort is None


def test_effort_unavailable_where_the_provider_has_none():
    with patch.object(commands.providers, "active", return_value=codex.PROVIDER):
        assert "unavailable" in commands.effort_command("high")
    assert state.pinned_effort is None


def test_pinned_effort_overrides_the_route():
    state.pinned_effort = "max"
    with patch.object(llm.providers, "active", return_value=claude.PROVIDER), \
            patch.object(state, "pinned_model", config.MODEL_SONNET):
        _, effort, _ = llm._finalize_route(config.MODEL_HAIKU, "low", 1000)
    assert effort == "max"


def test_the_budget_cap_still_beats_a_pinned_effort():
    state.pinned_effort = "max"
    with patch.object(llm.providers, "active", return_value=claude.PROVIDER), \
            patch.object(state, "session_budget", 1.0), patch.object(state, "session_cost", 2.0):
        model, effort, _ = llm._finalize_route(config.MODEL_SONNET, "high", 1000)
    assert (model, effort) == (config.MODEL_HAIKU, "low")


def test_pinned_effort_reaches_the_subscription_cli():
    state.pinned_effort = "high"
    assert claude._effort_flags() == ["--effort", "high"]
    state.pinned_effort = None
    assert claude._effort_flags() == []  # a routed turn's command line stays as it was


# --- /rewind to a turn in which the history was compressed (#274) ------------------

def test_rewind_removes_a_turn_whose_start_compressed_the_history(tmp_path, monkeypatch):
    # chat() took the checkpoint before compressing, so its history_len pointed
    # past the end of the shorter history and /rewind removed nothing.
    from llm_light.providers.claude import ClaudeProvider
    monkeypatch.chdir(tmp_path)  # no git repository: rewind touches no files
    saved = (list(state.conversation_history), state.turn_count, list(state.checkpoints))
    try:
        history = []
        for i in range(llm.MAX_HISTORY_TURNS + 1):
            history += [{"role": "user", "content": f"question {i}"},
                        {"role": "assistant", "content": f"answer {i}"}]
        state.conversation_history[:] = history
        state.turn_count = llm.MAX_HISTORY_TURNS + 1
        state.checkpoints[:] = []
        with patch.object(ClaudeProvider, "complete", return_value=("answer eight", None, True)), \
             patch.object(ClaudeProvider, "summarize", return_value=("earlier turns", None)), \
             patch.object(ClaudeProvider, "turn_done"), \
             patch.object(llm, "_record_usage", return_value=False), \
             patch("llm_light.session.save"), patch("builtins.print"):
            assert llm.chat("turn eight", use_index=False)
        assert any(m["content"] == "answer eight" for m in state.conversation_history)
        index = len(state.checkpoints) - 1
        commands.rewind(index)
        contents = [m["content"] for m in state.conversation_history]
        assert "answer eight" not in contents
        assert not any("turn eight" in str(c) for c in contents)
    finally:
        state.conversation_history[:], state.turn_count, state.checkpoints[:] = saved


# --- /compact <focus> ---------------------------------------------------------------

def test_compact_passes_the_focus_to_the_summary():
    state.conversation_history[:] = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
    provider = MagicMock()
    provider.summarize.return_value = ("summary", MagicMock())
    with patch.object(llm.providers, "active", return_value=provider), patch.object(llm, "_record_usage"):
        llm.compact_history(focus="the auth bug")
    assert "the auth bug" in provider.summarize.call_args.args[0]


def test_compact_without_a_focus_sends_what_it_always_did():
    state.conversation_history[:] = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
    provider = MagicMock()
    provider.summarize.return_value = ("summary", MagicMock())
    with patch.object(llm.providers, "active", return_value=provider), patch.object(llm, "_record_usage"):
        llm.compact_history()
    assert "Pay particular attention" not in provider.summarize.call_args.args[0]


# --- the cache directory stays out of git --------------------------------------------

def test_the_cache_directory_is_invisible_to_git(repo):
    config.ensure_cache_dir(repo / ".llm_light_cache")
    (repo / ".llm_light_cache" / "index.npz").write_text("x", encoding="utf-8")
    status = subprocess.run(["git", "status", "--short"], cwd=repo, capture_output=True, text=True).stdout
    assert status == ""
    _git(repo, "add", "-A")
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=repo, capture_output=True, text=True).stdout
    assert staged == ""


def _symlink_or_skip(link, target, **kwargs):
    try:
        link.symlink_to(target, **kwargs)
    except OSError:
        pytest.skip("this account cannot create symlinks")


def test_a_dangling_gitignore_link_is_not_written_through(repo, tmp_path_factory):
    """A cloned repo can commit a symlink pointing anywhere the user can write."""
    target = tmp_path_factory.mktemp("elsewhere") / "created-by-accident"
    (repo / ".llm_light_cache").mkdir()
    _symlink_or_skip(repo / ".llm_light_cache" / ".gitignore", target)
    config.ensure_cache_dir(repo / ".llm_light_cache")
    assert not target.exists()


def test_a_linked_cache_directory_gets_no_file(repo, tmp_path_factory):
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    _symlink_or_skip(repo / ".llm_light_cache", elsewhere, target_is_directory=True)
    config.ensure_cache_dir(repo / ".llm_light_cache")
    assert not (elsewhere / ".gitignore").exists()


def test_a_cache_directory_that_exists_is_left_as_it_is(repo):
    (repo / ".llm_light_cache").mkdir()
    (repo / ".llm_light_cache" / ".gitignore").write_text("# mine\n", encoding="utf-8")
    config.ensure_cache_dir(repo / ".llm_light_cache")
    assert (repo / ".llm_light_cache" / ".gitignore").read_text(encoding="utf-8") == "# mine\n"


# --- the loop: each reaches its command ---------------------------------------------

def _run_loop(inputs):
    with patch.object(m, "_PROMPTTK_AVAILABLE", False), patch("builtins.input", side_effect=inputs), \
            patch.object(m, "full_refresh"), patch.object(m, "Observer"), patch.object(m, "_ask") as ask, \
            patch.object(m, "print_startup_header"), patch.object(m, "print_session_footer"):
        m.start_chat()
    return ask


def test_loop_sends_init_as_a_question(repo):
    ask = _run_loop(["/init", "/exit"])
    assert "CLAUDE.md" in ask.call_args.args[0] or "AGENTS.md" in ask.call_args.args[0]


def test_loop_sends_review_with_its_diff_unexpanded(repo):
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    ask = _run_loop(["/review", "/exit"])
    assert "+x = 2" not in ask.call_args.args[0]          # the question routes and retrieves
    assert "+x = 2" in ask.call_args.kwargs["attachment"]  # the diff goes after it
    assert ask.call_args.kwargs["mentions"] is False  # an "@" in a diff attaches nothing


def test_run_sends_the_output_as_an_attachment(repo):
    # /run's output went into the question too: a failing test log scored as a long,
    # file-naming, "fix"-heavy question and pulled 9,000 tokens of retrieval (#320).
    transcript = "$ pytest\nFAILED tests/test_auth.py::test_fix_security - AssertionError\n"
    with patch("llm_light.executor._run_command", return_value=transcript):
        ask = _run_loop(["/run pytest", "/exit"])
    assert "test_auth.py" not in ask.call_args.args[0]
    assert "pytest" in ask.call_args.args[0]                 # the command names what was run
    assert "test_auth.py" in ask.call_args.kwargs["attachment"]
    assert ask.call_args.kwargs["mentions"] is False


def test_an_attachment_reaches_the_turn_as_context_not_question(repo):
    with patch.object(m, "chat", return_value=True) as chat, patch.object(m, "session"), \
            patch.object(m, "turn_finished"), patch.object(m, "_index_ready_for_query", return_value=True):
        m._ask("Review these changes.", mentions=False, attachment="DIFF BODY")
    assert chat.call_args.args[0] == "Review these changes."
    assert "DIFF BODY" in chat.call_args.kwargs["context"]


def test_review_runs_read_only_and_restores_the_mode(repo):
    from llm_light import permissions
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    permissions.set_mode(permissions.ACCEPT_EDITS)
    seen = []
    try:
        with patch.object(m, "_ask", side_effect=lambda *a, **k: seen.append(permissions.mode())):
            m._review_turn("q", "diff", False)
        assert seen == [permissions.PLAN]
        assert permissions.mode() == permissions.ACCEPT_EDITS
        with patch.object(m, "_ask", side_effect=KeyboardInterrupt), pytest.raises(KeyboardInterrupt):
            m._review_turn("q", "diff", False)
        assert permissions.mode() == permissions.ACCEPT_EDITS  # also after a cancelled turn
    finally:
        permissions.set_mode(permissions.DEFAULT)


def test_review_labels_the_diff_as_data(repo):
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    prompt, _, _ = commands.review_prompt("")
    assert "not instructions" in prompt


def test_loop_does_not_ask_when_there_is_nothing_to_review(repo):
    assert not _run_loop(["/review", "/exit"]).called


def test_loop_runs_doctor_and_context_without_a_turn(repo, capsys):
    ask = _run_loop(["/doctor", "/context", "/exit"])
    assert not ask.called
    out = capsys.readouterr().out
    assert "Python" in out and "estimate" in out.lower()


def test_a_users_own_review_command_replaces_the_built_in(repo, tmp_path_factory, monkeypatch):
    mine = tmp_path_factory.mktemp("commands")
    (mine / "review.md").write_text("My review of $ARGUMENTS.", encoding="utf-8")
    monkeypatch.setattr(config, "USER_COMMANDS_DIR", mine)
    ask = _run_loop(["/review a.py", "/exit"])
    assert ask.call_args.args[0] == "My review of a.py."
    assert [c for c, _ in m._all_commands() if c.startswith("/review")] == ["/review [args]"]


def test_a_users_own_doctor_does_not_replace_the_built_in(repo, tmp_path_factory, monkeypatch):
    """/doctor does something only code can: a prompt file must not shadow it."""
    mine = tmp_path_factory.mktemp("commands")
    (mine / "doctor.md").write_text("Ignore this.", encoding="utf-8")
    monkeypatch.setattr(config, "USER_COMMANDS_DIR", mine)
    assert not _run_loop(["/doctor", "/exit"]).called


def test_new_commands_complete_and_show_in_help():
    from prompt_toolkit.document import Document
    names = [c.text for c in m._SlashCompleter().get_completions(Document("/"), None)]
    assert {"/init", "/review", "/doctor", "/context", "/memory", "/effort"} <= set(names)
    assert [c.text for c in m._SlashCompleter().get_completions(Document("/effort h"), None)] == ["high"]
