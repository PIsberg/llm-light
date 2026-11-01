"""
Tests for llm_light.providers.codex (specs/001-gemini-codex-providers: T015).

Covers US1 scenario 2, US2 scenarios 1-2, US4, FR-007, FR-010, FR-011 and the
missing-tool and not-logged-in edge cases. argv and messages come from
contracts/provider.md; event shapes from research.md R1. The real codex CLI is
never started: run_cli is patched.
"""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import llm_light  # noqa: F401  (loads the package before the submodules)
from llm_light import git_manager, llm, state
from llm_light.providers import codex
from llm_light.providers.base import ProviderError, transcript

_RUN = "llm_light.providers.codex.run_cli"
_WHICH = "llm_light.providers.codex.shutil.which"

_EXPECTED_ARGV_TAIL = [
    "exec", "--json", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config",
    "--sandbox", "read-only",
    "--disable", "shell_tool", "--disable", "unified_exec", "--disable", "apps", "-",
]

# Token trim (measured with codex-cli 0.166.0, see docs/BENCHMARKS.md): codex's own
# base instructions, skills list, permissions text, collaboration-mode text and
# environment block, plus the tool schemas of features an answer engine never uses.
_TRIM_KEYS = [
    "skills.include_instructions=false",
    "include_permissions_instructions=false",
    "include_collaboration_mode_instructions=false",
    "include_environment_context=false",
]
_TRIM_FEATURES = [
    "multi_agent", "image_generation", "browser_use", "computer_use", "goals", "plugins",
    "skill_search", "tool_suggest", "view_image", "sleep_tool", "in_app_browser",
    "workspace_dependencies",
]


def _config_values(argv):
    """The values of every `-c` flag in argv, in order."""
    return [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "-c"]


def _without_config(argv):
    """argv with every `-c <value>` pair removed."""
    out, skip = [], False
    for a in argv:
        if skip:
            skip = False
        elif a == "-c":
            skip = True
        else:
            out.append(a)
    return out


def _instructions_path(argv):
    prefix = "model_instructions_file="
    values = [v[len(prefix):] for v in _config_values(argv) if v.startswith(prefix)]
    assert len(values) == 1, f"expected one model_instructions_file, got {values}"
    return json.loads(values[0])


def _event(type_, **kw):
    return json.dumps({"type": type_, **kw})


def _msg(text):
    return _event("item.completed", item={"id": "i", "type": "agent_message", "text": text})


def _jsonl(*texts):
    lines = [_event("thread.started", thread_id="t"), _event("turn.started")]
    lines += [_msg(t) for t in texts]
    lines.append(_event("turn.completed", usage={"input_tokens": 1, "output_tokens": 1}))
    return "\n".join(lines) + "\n"


def _request():
    return {
        "model": "claude-sonnet-4-6",
        "max_tokens": 4096,
        "system": [{"type": "text", "text": "SYS ONE"}, {"type": "text", "text": "SYS TWO"}],
        "messages": [
            {"role": "user", "content": "earlier question"},
            {"role": "assistant", "content": "earlier answer"},
            {"role": "user", "content": [{"type": "text", "text": "the question"}]},
        ],
    }


def _complete(stdout=None, request=None):
    req = request or _request()
    out = _jsonl("an answer") if stdout is None else stdout
    with patch(_WHICH, return_value="/bin/codex"), patch(_RUN, return_value=out) as run:
        result = codex.PROVIDER.complete(req)
    return result, run, req


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


# --- identity ----------------------------------------------------------------

def test_us1s2_codex_identity_and_label():
    p = codex.PROVIDER
    assert codex.BINARY == "codex"
    assert p.name == "codex"
    assert set(p.capabilities) == set()
    assert p.model_label("claude-opus-4") == "CLI default model"
    assert p.header() == ("CLI default model", "codex CLI")


# --- complete: argv, stdin, reply (US1 s2, FR-007, FR-010) --------------------

def test_us1s2_complete_returns_agent_message_with_no_usage_and_not_printed():
    (reply, usage, printed), _, _ = _complete(_jsonl("an answer"))
    assert reply == "an answer"
    assert usage is None
    assert printed is False


def test_us1s2_reply_is_the_last_agent_message_after_narration():
    (reply, _u, _p), _, _ = _complete(_jsonl("Let me look.", "Still looking.", "final answer"))
    assert reply == "final answer"


def test_us1s2_non_json_lines_are_skipped():
    out = "warning: something\n" + _jsonl("the answer") + "not json {\n"
    (reply, _u, _p), _, _ = _complete(out)
    assert reply == "the answer"


def test_fr011_output_without_agent_message_raises_provider_error():
    out = "\n".join([_event("thread.started", thread_id="t"),
                     _event("item.completed", item={"type": "reasoning", "text": "hmm"}),
                     _event("turn.completed", usage={})]) + "\n"
    with pytest.raises(ProviderError) as exc:
        _complete(out)
    assert "codex" in str(exc.value)
    assert "output could not be parsed" in str(exc.value)
    assert str(exc.value).endswith("Update codex (npm install -g @openai/codex) and run it on its own to see what it prints.")  # #113: every message names a remedy


def test_fr011_empty_stdout_raises_provider_error():
    with pytest.raises(ProviderError) as exc:
        _complete("")
    assert "output could not be parsed" in str(exc.value)
    assert str(exc.value).endswith("Update codex (npm install -g @openai/codex) and run it on its own to see what it prints.")  # #113: every message names a remedy


def test_us1s2_complete_stdin_is_the_transcript_joined_by_blank_line():
    _, run, req = _complete()
    system_text, prompt = transcript(req)
    args, _kw = run.call_args
    assert args[0] == "codex"
    assert args[2] == system_text + "\n\n" + prompt


def test_fr010_argv_is_the_contract_argv():
    _, run, _ = _complete()
    argv = run.call_args[0][1]
    assert _without_config(argv) == ["/bin/codex"] + _EXPECTED_ARGV_TAIL


# --- token trim: codex's own prompt (docs/BENCHMARKS.md, codex prompt trim) ---

def _capture_instructions(seen):
    import os

    def fake(provider, argv, stdin_text, *a, **kw):
        path = _instructions_path(argv)
        seen["path"] = path
        seen["text"] = open(path, encoding="utf-8").read() if os.path.isfile(path) else None
        return _jsonl("x")
    return fake


@pytest.mark.parametrize("method", ["complete", "summarize"])
def test_trim_codex_base_instructions_are_replaced_by_a_no_tools_file(method):
    import os
    seen = {}
    with patch(_WHICH, return_value="/bin/codex"),          patch(_RUN, side_effect=_capture_instructions(seen)):
        if method == "complete":
            codex.PROVIDER.complete(_request())
        else:
            codex.PROVIDER.summarize("Summarize: X")
    # The file existed while codex ran, held the no-tools text, and is gone afterwards.
    assert seen["text"] == codex.INSTRUCTIONS
    assert "no tools" in codex.INSTRUCTIONS
    assert os.path.isabs(seen["path"])
    assert not os.path.exists(seen["path"])


def test_trim_instructions_file_is_removed_when_codex_fails():
    import os
    seen = {}

    def fake(provider, argv, stdin_text, *a, **kw):
        seen["path"] = _instructions_path(argv)
        raise ProviderError("boom")
    with patch(_WHICH, return_value="/bin/codex"), patch(_RUN, side_effect=fake):
        with pytest.raises(ProviderError):
            codex.PROVIDER.complete(_request())
    assert not os.path.exists(seen["path"])


def test_trim_instructions_file_is_not_in_the_cli_working_directory():
    import os
    seen = {}

    def fake(provider, argv, stdin_text, *a, **kw):
        seen["path"] = _instructions_path(argv)
        seen["cwd"] = kw["cwd"]
        return _jsonl("x")
    with patch(_WHICH, return_value="/bin/codex"), patch(_RUN, side_effect=fake):
        codex.PROVIDER.complete(_request())
    cwd = os.path.realpath(seen["cwd"])
    assert os.path.commonpath([cwd, os.path.realpath(seen["path"])]) != cwd


def test_trim_codex_boilerplate_sections_are_switched_off():
    _, run, _ = _complete()
    values = _config_values(run.call_args[0][1])
    for key in _TRIM_KEYS:
        assert key in values


def test_trim_unused_codex_features_are_switched_off_with_config_not_disable():
    # `--disable <unknown>` makes codex exit 1, so a feature a later codex renames
    # would break every call; `-c features.<unknown>=false` is ignored instead.
    _, run, _ = _complete()
    argv = run.call_args[0][1]
    values = _config_values(argv)
    for feature in _TRIM_FEATURES:
        assert f"features.{feature}=false" in values
    disabled = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--disable"]
    assert disabled == ["shell_tool", "unified_exec", "apps"]


def test_trim_config_flags_come_before_the_stdin_dash():
    _, run, _ = _complete()
    argv = run.call_args[0][1]
    assert argv.index("-") == len(argv) - 1


def test_fr007_argv_has_no_model_flag_and_no_claude_model_id():
    _, run, _ = _complete()
    argv = run.call_args[0][1]
    assert "-m" not in argv and "--model" not in argv
    assert not any(str(a).startswith("claude-") for a in argv)
    assert "claude-sonnet-4-6" not in run.call_args[0][2]


def test_fr011_run_cli_error_passes_through_unchanged():
    err = ProviderError("codex failed (exit 1): boom. Run codex on its own to see the full error.")
    with patch(_WHICH, return_value="/bin/codex"), patch(_RUN, side_effect=err):
        with pytest.raises(ProviderError) as exc:
            codex.PROVIDER.complete(_request())
    assert exc.value is err


# --- summarize (contract table) ----------------------------------------------

def test_summarize_uses_same_argv_with_summary_prefix_and_no_usage():
    with patch(_WHICH, return_value="/bin/codex"), \
         patch(_RUN, return_value=_jsonl("the summary")) as run:
        text, usage = codex.PROVIDER.summarize("Summarize: X")
    assert text == "the summary"
    assert usage is None
    args, _kw = run.call_args
    assert _without_config(args[1]) == ["/bin/codex"] + _EXPECTED_ARGV_TAIL
    assert args[2] == "You summarize conversations.\n\nSummarize: X"


# --- require_auth (FR-011, edge cases) ---------------------------------------

def test_edge_missing_tool_exits_1_with_install_message(clean_env, capsys):
    with patch(_WHICH, return_value=None):
        with pytest.raises(SystemExit) as exc:
            codex.PROVIDER.require_auth()
    assert exc.value.code == 1
    out = capsys.readouterr()
    assert "codex CLI not found. Install it: npm install -g @openai/codex" in out.err + out.out


def test_edge_not_logged_in_exits_1_with_login_message(clean_env, capsys):
    with patch(_WHICH, return_value="/bin/codex"):
        with pytest.raises(SystemExit) as exc:
            codex.PROVIDER.require_auth()
    assert exc.value.code == 1
    out = capsys.readouterr()
    assert "codex is not logged in. Run: codex login" in out.err + out.out


def test_fr011_default_auth_file_under_home_passes_require_auth(clean_env):
    auth = clean_env / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text("{}", encoding="utf-8")
    with patch(_WHICH, return_value="/bin/codex"):
        codex.PROVIDER.require_auth()


def test_fr011_codex_home_is_honoured(clean_env, monkeypatch):
    other = clean_env / "elsewhere"
    other.mkdir()
    (other / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(other))
    with patch(_WHICH, return_value="/bin/codex"):
        codex.PROVIDER.require_auth()


def test_fr011_codex_home_without_auth_file_is_not_logged_in(clean_env, monkeypatch):
    # The default location has a login, but CODEX_HOME points somewhere without one.
    default = clean_env / ".codex" / "auth.json"
    default.parent.mkdir(parents=True)
    default.write_text("{}", encoding="utf-8")
    empty = clean_env / "empty_home"
    empty.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(empty))
    with patch(_WHICH, return_value="/bin/codex"):
        with pytest.raises(SystemExit) as exc:
            codex.PROVIDER.require_auth()
    assert exc.value.code == 1


# --- US2 / US4: an edit turn through llm.chat --------------------------------

@pytest.fixture
def git_repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def run(*a):
        subprocess.run(a, capture_output=True, check=True)

    run("git", "init")
    run("git", "config", "user.email", "test@example.com")
    run("git", "config", "user.name", "Test User")
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    run("git", "add", "app.py")
    run("git", "commit", "-m", "Initial commit")
    return tmp_path


@pytest.fixture
def codex_turn(monkeypatch, git_repo):
    monkeypatch.setattr(state, "conversation_history", [])
    monkeypatch.setattr(state, "turn_count", 0)
    monkeypatch.setattr(state, "skeleton_context", "SKELETON")
    monkeypatch.setattr(state, "provider", codex.PROVIDER, raising=False)
    monkeypatch.setattr(llm, "retrieve", lambda *a, **k: ("CHUNK TEXT", []))
    monkeypatch.setattr(llm, "route_query", lambda q: ("some-model", "medium", 4096))
    monkeypatch.setattr(llm, "_finalize_route", lambda m, e, t: (m, e, t))
    monkeypatch.setattr(llm, "record_retrieval", lambda *a, **k: None)
    monkeypatch.setattr(llm, "_maybe_compress_history", lambda: None)
    monkeypatch.setattr(llm, "print_stats", lambda *a, **k: None)
    monkeypatch.setattr(llm, "_print_reply", lambda *a, **k: None)
    monkeypatch.setattr(llm.config, "LLMLINGUA_ENABLED", False)
    return git_repo


_EDIT_REPLY = (
    "Changing x.\n"
    "```python:app.py\n"
    "<<<<<<< SEARCH\n"
    "x = 1\n"
    "=======\n"
    "x = 2\n"
    ">>>>>>> REPLACE\n"
    "```\n"
)


def _edit_turn():
    with patch(_WHICH, return_value="/bin/codex"), \
         patch(_RUN, return_value=_jsonl("Looking.", _EDIT_REPLY)):
        llm.chat("change x to 2", auto_apply=True)


def _head_message(repo):
    return subprocess.run(["git", "log", "-1", "--pretty=%s"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()


def test_us2s1_edit_reply_changes_file_and_commits_with_prefix(codex_turn):
    _edit_turn()
    assert (codex_turn / "app.py").read_text(encoding="utf-8").strip() == "x = 2"
    assert _head_message(codex_turn).startswith(git_manager.COMMIT_PREFIX)


def test_us2s2_undo_restores_the_file(codex_turn):
    _edit_turn()
    assert git_manager.undo_last_commit() is True
    assert (codex_turn / "app.py").read_text(encoding="utf-8").strip() == "x = 1"


def test_us4_edit_turn_runs_without_the_claude_and_gemini_modules(codex_turn, monkeypatch):
    monkeypatch.setitem(sys.modules, "llm_light.providers.claude", None)
    monkeypatch.setitem(sys.modules, "llm_light.providers.gemini", None)
    _edit_turn()
    assert (codex_turn / "app.py").read_text(encoding="utf-8").strip() == "x = 2"
    assert _head_message(codex_turn).startswith(git_manager.COMMIT_PREFIX)


# FR-010 / constitution V: the CLI must not run in the user's project, where a
# repo-shipped .codex/ config could execute code. It runs in a fresh empty temp dir.
def _capture_cwd_run(seen, error=None, stdout=None):
    import os

    def fake(provider, argv, stdin_text, *a, **kw):
        cwd = kw.get("cwd")
        seen["cwd"] = cwd
        seen["stdin"] = stdin_text
        assert cwd is not None, "run_cli called without cwd"
        seen["exists"] = os.path.isdir(cwd)
        seen["listing"] = os.listdir(cwd)
        if error:
            raise error
        return stdout
    return fake


@pytest.mark.parametrize("method", ["complete", "summarize"])
@pytest.mark.parametrize("fail", [False, True])
def test_fr010_cli_runs_in_fresh_empty_temp_dir_removed_afterwards(method, fail):
    import os
    seen = {}
    out = _jsonl("x")
    err = ProviderError("boom") if fail else None
    fake = _capture_cwd_run(seen, error=err, stdout=out)
    with patch(_WHICH, return_value="/bin/codex"), patch(_RUN, side_effect=fake):
        call = (lambda: codex.PROVIDER.complete(_request())) if method == "complete"             else (lambda: codex.PROVIDER.summarize("Summarize: X"))
        if fail:
            with pytest.raises(ProviderError):
                call()
        else:
            call()
    cwd = seen["cwd"]
    assert seen["exists"] is True
    assert seen["listing"] == []
    assert os.path.realpath(cwd) != os.path.realpath(os.getcwd())
    assert not os.path.exists(cwd)
