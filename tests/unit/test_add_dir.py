"""--add-dir and /add-dir: the subscription agent may read, but not write, directories
outside the project, such as a sibling repository.

Writes stay inside the project, where llm_light shows the diff and commits it so /undo
works; an edit in another directory would bypass both.
"""

import io
from unittest.mock import MagicMock, patch

import pytest

from llm_light import commands, config, llm, main, permissions
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


def test_api_key_question_has_no_note(project, other):
    # Its own tools cannot reach the directory (yet), so naming it would only mislead.
    commands.add_dir(str(other))
    with patch.object(llm, "_agent_oauth", return_value=False):
        assert llm._question_text("q", "") == "Question:\nq"


# --- startup --------------------------------------------------------------------------------

def test_flag_is_parsed():
    parser = main._build_parser()
    assert parser.parse_known_args(["--add-dir", "a", "--add-dir", "b"])[0].add_dir == ["a", "b"]
    assert parser.parse_known_args([])[0].add_dir == []


@pytest.mark.parametrize("auth_mode, provider, works", [
    ("OAUTH", "claude", True),
    ("API_KEY", "claude", False),
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
