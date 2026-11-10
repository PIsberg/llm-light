"""--add-dir and /add-dir: the agent may read, but not write, directories
outside the project, such as a sibling repository.

Writes stay inside the project, where llm_light shows the diff and commits it so /undo
works; an edit in another directory would bypass both.
"""

import io
import os
from unittest.mock import MagicMock, patch

import pytest

from llm_light import commands, config, llm, main, permissions, tools
from llm_light.providers import claude as _claude_mod


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.chdir(root)
    monkeypatch.setattr(config, "EXTRA_DIRS", [])
    monkeypatch.setattr(config, "ALLOWED_COMMANDS", [])
    monkeypatch.setattr(config, "WEB_ACCESS", False)
    yield root
    permissions.set_mode(permissions.DEFAULT)


@pytest.fixture
def other(tmp_path):
    path = tmp_path / "shared"
    path.mkdir()
    return path


@pytest.fixture
def mode():
    import llm_light.state as state
    allowed = state.bypass_allowed

    def set_mode(name):
        state.bypass_allowed = True
        permissions.set_mode(name)
    yield set_mode
    permissions.set_mode(permissions.DEFAULT)
    state.bypass_allowed = allowed


# --- the rule -------------------------------------------------------------------------------

@pytest.mark.parametrize("path, rule", [
    # Checked live (claude 2.1.322 on Windows): //c/... matched, //C:/... did not.
    ("C:\\Users\\me\\shared", "Read(//c/Users/me/shared/**)"),
    ("D:/work/lib", "Read(//d/work/lib/**)"),
    ("/home/me/shared", "Read(//home/me/shared/**)"),
])
def test_read_rule(path, rule):
    assert config.read_rule(path) == rule


# --- /add-dir -------------------------------------------------------------------------------

def test_add_dir_adds_it_read_only(project, other):
    message = commands.add_dir(str(other))
    assert config.EXTRA_DIRS == [str(other.resolve())]
    assert "read" in message and "next turn" in message


def test_add_dir_resolves_a_relative_path(project, other):
    commands.add_dir("../shared")
    assert config.EXTRA_DIRS == [str(other.resolve())]


def test_adding_twice_keeps_one(project, other):
    commands.add_dir(str(other))
    commands.add_dir(str(other))
    assert config.EXTRA_DIRS == [str(other.resolve())]


@pytest.mark.parametrize("make", [
    lambda project, other: other / "missing",
    lambda project, other: (other / "f.txt").write_text("x") and other / "f.txt",
    lambda project, other: project / "sub",          # inside the project already
    lambda project, other: project,
    lambda project, other: other.parent / "a,b",     # a comma would split --allowedTools
    lambda project, other: other.parent / "a(b)",    # a parenthesis would end the rule
])
def test_refused(project, other, make):
    path = make(project, other)
    if path.name in ("sub", "a,b", "a(b)"):
        path.mkdir()
    message = commands.add_dir(str(path))
    assert config.EXTRA_DIRS == []
    assert message.startswith("Not added")


def test_refuses_the_filesystem_root(project):
    message = commands.add_dir(project.anchor)
    assert config.EXTRA_DIRS == []
    assert message.startswith("Not added")


def test_without_an_argument_lists_them(project, other):
    assert "/add-dir <path>" in commands.add_dir("")
    commands.add_dir(str(other))
    assert str(other.resolve()) in commands.add_dir("")


# --- the CLI turn ---------------------------------------------------------------------------

def _cli_run(tools_on=True):
    seen = {}
    proc = MagicMock()
    proc.stdout = iter(['{"type":"result","subtype":"success","result":"","usage":{}}\n'])
    proc.stderr = io.StringIO("")
    proc.wait.return_value = proc.poll.return_value = proc.returncode = 0

    def popen(cmd, **kwargs):
        seen["cmd"] = cmd
        return proc
    with patch("llm_light.llm.subprocess.Popen", side_effect=popen), \
         patch("shutil.which", return_value="/fake/bin/claude"), \
         patch("sys.stdout", io.StringIO()):
        _claude_mod._make_cli_subprocess_call("hi", tools=tools_on)
    return seen["cmd"]


def _rules(cmd):
    return cmd[cmd.index("--allowedTools") + 1].split(",") if "--allowedTools" in cmd else []


@pytest.mark.parametrize("name", [permissions.DEFAULT, permissions.ACCEPT_EDITS, permissions.PLAN])
def test_turn_may_read_the_directory(project, other, mode, name):
    mode(name)
    commands.add_dir(str(other))
    cmd = _cli_run()
    assert config.read_rule(str(other.resolve())) in _rules(cmd)
    assert not any(r.startswith(("Edit(", "Write(")) for r in _rules(cmd))
    assert "--add-dir" not in cmd  # it would let the agent write there too


def test_without_extra_dirs_there_is_no_read_rule(project, mode):
    mode(permissions.ACCEPT_EDITS)
    assert not any(r.startswith("Read(") for r in _rules(_cli_run()))


def test_bypass_needs_no_rule(project, other, mode):
    mode(permissions.BYPASS)
    commands.add_dir(str(other))
    assert "--allowedTools" not in _cli_run()


def test_hidden_call_gets_no_rule(project, other):
    commands.add_dir(str(other))
    assert "--allowedTools" not in _cli_run(tools_on=False)


# --- telling the model ----------------------------------------------------------------------

def test_question_names_the_directories_for_the_agent(project, other):
    commands.add_dir(str(other))
    with patch.object(llm, "_agent_oauth", return_value=True):
        text = llm._question_text("what is in shared?", "")
    assert str(other.resolve()) in text and "read-only" in text


def test_question_is_unchanged_without_extra_dirs(project):
    with patch.object(llm, "_agent_oauth", return_value=True):
        assert llm._question_text("q", "") == "Question:\nq"


def test_api_key_question_says_how_to_reach_the_directories(project, other):
    # llm_light's own tools take project-relative paths, so the model has to be told that an
    # absolute path, or a grep glob that starts with the directory, reaches it.
    commands.add_dir(str(other))
    with patch.object(llm, "_agent_oauth", return_value=False):
        text = llm._question_text("q", "")
    shown = other.resolve().as_posix()
    assert shown in text and "read-only" in text and f"'{shown}/*'" in text


def test_api_key_question_is_unchanged_without_extra_dirs(project):
    with patch.object(llm, "_agent_oauth", return_value=False):
        assert llm._question_text("q", "") == "Question:\nq"


@pytest.mark.parametrize("name", ["gemini", "codex"])
def test_no_note_for_a_provider_without_file_tools(project, other, monkeypatch, name):
    from llm_light import providers
    commands.add_dir(str(other))
    monkeypatch.setattr(providers, "active", lambda: providers.get(name))
    with patch.object(llm, "_agent_oauth", return_value=False):
        assert llm._question_text("q", "") == "Question:\nq"


# --- startup --------------------------------------------------------------------------------

def test_flag_is_parsed():
    parser = main._build_parser()
    assert parser.parse_known_args(["--add-dir", "a", "--add-dir", "b"])[0].add_dir == ["a", "b"]
    assert parser.parse_known_args([])[0].add_dir == []


@pytest.mark.parametrize("auth_mode, provider, works", [
    ("OAUTH", "claude", True),
    ("API_KEY", "claude", True),
    ("API_KEY", "codex", False),
    ("OAUTH", "codex", False),
])
def test_startup_note_where_it_has_no_effect(project, other, monkeypatch, auth_mode, provider, works):
    from llm_light import providers
    commands.add_dir(str(other))
    monkeypatch.setattr(main, "AUTH_MODE", auth_mode)
    note = main._add_dir_note(providers.get(provider))
    assert (note is None) is works
    if note:
        assert "--add-dir" in note


# --- API-key tools --------------------------------------------------------------------------

@pytest.fixture
def shared(project, other):
    (project / "app.py").write_text("from shared import helper\n", encoding="utf-8")
    (other / "lib").mkdir()
    (other / "lib" / "helper.py").write_text("PLANTED = 'kumquat-7'\n", encoding="utf-8")
    (other / ".git").mkdir()
    (other / ".git" / "config").write_text("PLANTED in git internals\n", encoding="utf-8")
    (other.parent / "secret.txt").write_text("PLANTED kept-out-9\n", encoding="utf-8")
    return other


def _tool(project, name, tool_input):
    return tools.run_tool(name, tool_input, root=project)


def test_read_file_reaches_an_added_directory_by_absolute_path(project, shared):
    path = (shared / "lib" / "helper.py").resolve().as_posix()
    assert _tool(project, "read_file", {"path": path})[1] is True  # not added yet
    commands.add_dir(str(shared))
    out, is_error = _tool(project, "read_file", {"path": path})
    assert not is_error and "kumquat-7" in out


def test_read_file_still_refuses_what_is_outside_both(project, shared):
    commands.add_dir(str(shared))
    for path in ((shared.parent / "secret.txt").resolve().as_posix(),
                 (shared / ".." / "secret.txt").as_posix()):
        out, is_error = _tool(project, "read_file", {"path": path})
        assert is_error and "outside" in out and "kept-out-9" not in out


def test_list_files_in_an_added_directory_gives_absolute_paths(project, shared):
    commands.add_dir(str(shared))
    out, is_error = _tool(project, "list_files", {"path": str(shared)})
    assert not is_error
    assert out.splitlines() == [(shared / "lib" / "helper.py").resolve().as_posix()]


def test_grep_searches_an_added_directory_when_the_glob_starts_with_it(project, shared):
    commands.add_dir(str(shared))
    root = shared.resolve().as_posix()
    out, _ = _tool(project, "grep", {"pattern": "PLANTED", "glob": root + "/*"})
    assert out == f"{root}/lib/helper.py:1: PLANTED = 'kumquat-7'"
    # The directory alone, or written with the platform's separator, names it too.
    for glob in (root, str(shared.resolve()) + os.sep + "*.py"):
        assert "kumquat-7" in _tool(project, "grep", {"pattern": "kumquat", "glob": glob})[0]


def test_grep_without_such_a_glob_stays_in_the_project(project, shared):
    commands.add_dir(str(shared))
    out, _ = _tool(project, "grep", {"pattern": "PLANTED|shared"})
    assert out == "app.py:1: from shared import helper"


def test_project_paths_stay_relative(project, shared):
    commands.add_dir(str(shared))
    assert _tool(project, "list_files", {})[0] == "app.py"
    assert _tool(project, "read_file", {"path": "app.py"})[0] == "1\tfrom shared import helper"


def test_a_symlink_out_of_an_added_directory_is_not_followed(project, shared):
    # The project's own tools refuse the same thing (test_tools.py); an added directory is
    # a second root and gets the same check against itself.
    link = shared / "lib" / "innocent.txt"
    try:
        os.symlink(shared.parent / "secret.txt", link)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"cannot create symlinks here: {e}")
    commands.add_dir(str(shared))
    root = shared.resolve().as_posix()
    assert "kept-out-9" not in _tool(project, "grep", {"pattern": "kept-out", "glob": root + "/*"})[0]
    assert "innocent" not in _tool(project, "list_files", {"path": root})[0]
    out, is_error = _tool(project, "read_file", {"path": link.as_posix()})
    assert is_error and "kept-out-9" not in out
