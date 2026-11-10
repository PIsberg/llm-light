"""~/.llm_light/instructions.md: the user's own instructions, sent with every question in
every project, as ~/.claude/CLAUDE.md, ~/.codex/AGENTS.md and ~/.gemini/GEMINI.md are for
those tools.

llm_light hides ~/.claude/CLAUDE.md from its agent on purpose: it is written for Claude
Code and names tools llm_light does not have. This file is llm_light's own.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

import llm_light.state as state
from llm_light import commands, config, instructions, llm, main
from llm_light.providers.base import transcript


@pytest.fixture
def user_file(tmp_path, monkeypatch):
    path = tmp_path / "home" / ".llm_light" / "instructions.md"
    monkeypatch.setenv("LLM_LIGHT_INSTRUCTIONS", str(path))
    monkeypatch.setattr(state, "extra_system", "")

    def write(text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path
    write.path = path
    return write


def _system_text():
    return llm._build_system_blocks("SKELETON")[0]["text"]


def test_the_suite_never_reads_the_real_file():
    # tests/conftest.py points the variable at a temp path, as it does for mcp.json.
    assert instructions.path() != Path.home() / ".llm_light" / "instructions.md"


def test_default_path_is_in_the_home_directory(monkeypatch):
    monkeypatch.delenv("LLM_LIGHT_INSTRUCTIONS", raising=False)
    assert instructions.path() == Path.home() / ".llm_light" / "instructions.md"


def test_without_the_file_the_request_is_unchanged(user_file):
    assert _system_text() == config.SYSTEM_PROMPT


@pytest.mark.parametrize("text", ["", "  \n\n"])
def test_an_empty_file_changes_nothing(user_file, text):
    user_file(text)
    assert _system_text() == config.SYSTEM_PROMPT


def test_the_file_follows_the_system_prompt(user_file):
    user_file("Answer in British English.\n")
    text = _system_text()
    assert text.startswith(config.SYSTEM_PROMPT)
    assert "Answer in British English." in text
    blocks = llm._build_system_blocks("SKELETON")
    assert blocks[1]["text"] == "SKELETON"  # the cached skeleton block is untouched


def test_append_system_prompt_comes_after_it(user_file, monkeypatch):
    user_file("FROM-THE-FILE")
    monkeypatch.setattr(state, "extra_system", "FROM-THE-FLAG")
    text = _system_text()
    assert text.index("FROM-THE-FILE") < text.index("FROM-THE-FLAG")


def test_a_long_file_is_cut(user_file):
    user_file("x" * (instructions.MAX_CHARS + 500))
    text = instructions.text()
    assert len(text) < instructions.MAX_CHARS + 200
    assert "cut" in text


def test_a_projects_own_file_is_never_read(user_file, tmp_path, monkeypatch):
    # A cloned repo must not be able to add instructions this way.
    project = tmp_path / "project"
    (project / ".llm_light").mkdir(parents=True)
    (project / ".llm_light" / "instructions.md").write_text("FROM-THE-REPO", encoding="utf-8")
    monkeypatch.chdir(project)
    assert "FROM-THE-REPO" not in _system_text()


def test_cli_providers_get_it_through_the_transcript(user_file):
    # Gemini and Codex flatten the system blocks; the claude CLI swaps only the
    # SYSTEM_PROMPT part for its agent prompt, so the rest survives.
    user_file("FROM-THE-FILE")
    system_text, _ = transcript({"system": llm._build_system_blocks("SKELETON"),
                                 "messages": [{"role": "user", "content": "q"}]})
    assert "FROM-THE-FILE" in system_text
    swapped = system_text.replace(config.SYSTEM_PROMPT, config.AGENT_SYSTEM_PROMPT, 1)
    assert "FROM-THE-FILE" in swapped


def test_startup_line_names_the_file(user_file):
    assert instructions.startup_line() is None
    user_file("one\ntwo\n")
    line = instructions.startup_line()
    assert str(user_file.path) in line and "2 lines" in line


def test_memory_user_opens_it_and_creates_the_folder(user_file):
    with patch.object(commands, "editor_argv", return_value=["fake-editor"]), \
         patch.object(commands.subprocess, "call", return_value=0) as call:
        message = commands.memory_command(lambda *a: None, "user")
    assert call.call_args[0][0] == ["fake-editor", str(user_file.path)]
    assert user_file.path.is_file()
    assert "every project" in message


def test_memory_without_an_argument_still_opens_the_project_file(user_file, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("x", encoding="utf-8")
    with patch.object(commands, "editor_argv", return_value=["fake-editor"]), \
         patch.object(commands.subprocess, "call", return_value=0) as call:
        commands.memory_command(lambda *a: None)
    assert call.call_args[0][0] == ["fake-editor", "CLAUDE.md"]


def test_help_names_memory_user():
    assert any(cmd.startswith("/memory") and "user" in cmd + desc for cmd, desc in main._COMMANDS)
