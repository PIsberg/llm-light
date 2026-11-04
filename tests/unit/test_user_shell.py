"""Which shell runs a command line: Git Bash on Windows when it is installed (#193)."""

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from llm_light import config, executor, permissions, tools, user_shell


@pytest.fixture(autouse=True)
def _no_cached_lookup(monkeypatch):
    monkeypatch.delenv("LLM_LIGHT_SHELL", raising=False)
    user_shell.git_bash.cache_clear()
    yield
    user_shell.git_bash.cache_clear()


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    return path


def _windows(path=(), env=None):
    """Pretend to be on Windows, with `path` as PATH and no standard install."""
    return (patch.object(user_shell, "_on_windows", return_value=True),
            patch.dict(os.environ, {"ProgramFiles": "", "ProgramW6432": "", "ProgramFiles(x86)": "",
                                    "LocalAppData": "", "PATH": os.pathsep.join(str(p) for p in path),
                                    **(env or {})}))


def _run_in_fake_windows(path=(), env=None):
    a, b = _windows(path, env)
    with a, b:
        return user_shell.command_argv("echo hi")


def test_posix_keeps_the_system_shell():
    with patch.object(user_shell, "_on_windows", return_value=False):
        assert user_shell.command_argv("echo hi") == ("echo hi", True)


def test_finds_bash_beside_git_for_windows(tmp_path):
    git = _touch(tmp_path / "Git" / "cmd" / "git.exe")
    bash = _touch(tmp_path / "Git" / "bin" / "bash.exe")
    assert _run_in_fake_windows([git.parent]) == ([str(bash), "-c", "echo hi"], False)


def test_finds_bash_from_the_mingw_git(tmp_path):
    git = _touch(tmp_path / "Git" / "mingw64" / "bin" / "git.exe")
    bash = _touch(tmp_path / "Git" / "bin" / "bash.exe")
    assert _run_in_fake_windows([git.parent]) == ([str(bash), "-c", "echo hi"], False)


def test_finds_bash_in_program_files_when_git_is_not_on_path(tmp_path):
    bash = _touch(tmp_path / "Git" / "bin" / "bash.exe")
    assert _run_in_fake_windows(env={"ProgramFiles": str(tmp_path)}) == ([str(bash), "-c", "echo hi"], False)


def test_never_takes_the_wsl_bash_from_path(tmp_path):
    """System32\\bash.exe is WSL: another filesystem, another PATH, usually no python."""
    wsl = _touch(tmp_path / "System32" / "bash.exe")
    assert _run_in_fake_windows([wsl.parent]) == ("echo hi", True)


def test_ignores_a_git_in_the_current_directory(tmp_path, monkeypatch):
    (tmp_path / "repo" / "sub").mkdir(parents=True)
    _touch(tmp_path / "repo" / "sub" / "git.exe")
    _touch(tmp_path / "repo" / "bin" / "bash.exe")
    monkeypatch.chdir(tmp_path / "repo" / "sub")
    assert _run_in_fake_windows([".", ""]) == ("echo hi", True)


def test_ignores_a_path_entry_inside_the_project(tmp_path, monkeypatch):
    git = _touch(tmp_path / "repo" / "cmd" / "git.exe")
    _touch(tmp_path / "repo" / "bin" / "bash.exe")
    monkeypatch.chdir(tmp_path / "repo")
    assert _run_in_fake_windows([git.parent]) == ("echo hi", True)


def test_git_outside_a_known_layout_is_ignored(tmp_path):
    """A git.exe elsewhere says nothing about a bash above it."""
    git = _touch(tmp_path / "tools" / "x" / "git.exe")
    _touch(tmp_path / "tools" / "bin" / "bash.exe")
    assert _run_in_fake_windows([git.parent]) == ("echo hi", True)


def test_falls_back_to_cmd_without_git_bash():
    assert _run_in_fake_windows() == ("echo hi", True)


def test_override_selects_cmd_even_with_git_bash(tmp_path):
    git = _touch(tmp_path / "Git" / "cmd" / "git.exe")
    _touch(tmp_path / "Git" / "bin" / "bash.exe")
    assert _run_in_fake_windows([git.parent], {"LLM_LIGHT_SHELL": "cmd"}) == ("echo hi", True)


def test_override_names_a_bash(tmp_path):
    bash = _touch(tmp_path / "mine" / "bash.exe")
    assert _run_in_fake_windows(env={"LLM_LIGHT_SHELL": str(bash)}) == ([str(bash), "-c", "echo hi"], False)


def test_override_that_does_not_exist_falls_back(tmp_path):
    assert _run_in_fake_windows(env={"LLM_LIGHT_SHELL": str(tmp_path / "missing.exe")}) == ("echo hi", True)


def test_name_says_which_shell_runs_commands(tmp_path):
    git = _touch(tmp_path / "Git" / "cmd" / "git.exe")
    _touch(tmp_path / "Git" / "bin" / "bash.exe")
    a, b = _windows([git.parent])
    with a, b:
        assert user_shell.name() == "Git Bash"
    user_shell.git_bash.cache_clear()
    a, b = _windows()
    with a, b:
        assert user_shell.name() == "cmd.exe"
    with patch.object(user_shell, "_on_windows", return_value=False):
        assert user_shell.name() == "sh"


# --- the real shell ---------------------------------------------------------------

_HAS_BASH = os.name != "nt" or Path(os.environ.get("ProgramFiles", ""), "Git", "bin", "bash.exe").exists()
needs_bash = pytest.mark.skipif(not _HAS_BASH, reason="Windows without Git for Windows runs cmd.exe")


@needs_bash
def test_typed_command_line_means_the_same_as_in_the_agents_bash(capsys):
    """Arithmetic expansion: cmd.exe prints it back literally, a POSIX shell computes it."""
    transcript = executor._run_command("echo $((40+2))")
    capsys.readouterr()
    assert "42" in transcript and "$((" not in transcript.split("\n", 2)[2]


@needs_bash
def test_percent_is_plain_text_in_the_shell_tool(tmp_path):
    """Allowed commands get no variable expansion, whichever way it is spelled."""
    with patch.dict(os.environ, {"LLM_LIGHT_TEST_VALUE": "expanded"}), \
            patch.object(config, "ALLOWED_COMMANDS", ["echo"]), \
            patch.object(permissions, "plan", return_value=False), \
            patch.object(permissions, "bypass", return_value=False):
        output, is_error = tools._shell(tmp_path, {"command": "echo %LLM_LIGHT_TEST_VALUE%"})
    assert not is_error
    assert "expanded" not in output


def test_cmd_fallback_refuses_percent_expansion(tmp_path):
    with patch.object(user_shell, "name", return_value="cmd.exe"),             patch.object(config, "ALLOWED_COMMANDS", ["echo"]),             patch.object(permissions, "plan", return_value=False),             patch.object(permissions, "bypass", return_value=False),             pytest.raises(tools._ToolError, match="may not contain '%'"):
        tools._shell(tmp_path, {"command": "echo %PATH%"})


@needs_bash
def test_shell_tool_kills_the_tree_on_timeout(tmp_path):
    """The bash wrapper starts a second bash on Windows; a timeout must end both."""
    with patch.object(config, "ALLOWED_COMMANDS", ["sleep"]), \
            patch.object(tools, "_shell_timeout", return_value=0.5), \
            patch.object(permissions, "plan", return_value=False), \
            patch.object(permissions, "bypass", return_value=False):
        started = time.monotonic()
        output, is_error = tools._shell(tmp_path, {"command": "sleep 20"})
    assert is_error and "TIMEOUT" in output
    assert time.monotonic() - started < 15
