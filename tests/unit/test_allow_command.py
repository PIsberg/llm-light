"""/allow and /disallow: change what the agent may run without restarting, and
remember it for the project, outside the repository."""

import json

import pytest

import llm_light.state as state
from llm_light import commands, config, main, permissions, session
from llm_light.providers import claude
from llm_light.tools import run_tool


@pytest.fixture
def allowlist(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path / "sessions")
    monkeypatch.setattr(config, "ALLOWED_COMMANDS", [])
    return project


def test_allow_takes_effect_without_a_restart(allowlist):
    assert run_tool("shell", {"command": "echo hi"}, root=allowlist)[1] is True
    message = commands.allow_command("echo")
    assert "echo" in message
    out, is_error = run_tool("shell", {"command": "echo hi"}, root=allowlist)
    assert not is_error and "hi" in out


def test_allow_gives_the_oauth_agent_its_shell_on_the_next_turn(allowlist):
    assert "Bash" not in claude._agent_tools()
    commands.allow_command("python -m pytest")
    assert "Bash" in claude._agent_tools()
    commands.allow_command("powershell: Invoke-Pester")
    assert "PowerShell" in claude._agent_tools()


def test_quotes_around_the_prefix_are_dropped(allowlist):
    commands.allow_command('"python -m pytest"')
    assert config.ALLOWED_COMMANDS == ["python -m pytest"]


def test_allowing_twice_keeps_one_entry(allowlist):
    commands.allow_command("echo")
    commands.allow_command("echo")
    assert config.ALLOWED_COMMANDS == ["echo"]
    assert json.loads(session.allowed_file().read_text(encoding="utf-8")) == ["echo"]


@pytest.mark.parametrize("prefix", [
    "echo a; rm x", "echo a && b", "echo | cat", "echo > f", "echo `id`", "echo $HOME",
    "foo,Bash", "foo)", "(foo", "*", "git *", "powershell: Write-Output (x)", "powershell: @x",
    "bash:", "powershell:   ",
])
def test_a_prefix_that_could_never_be_one_simple_command_is_refused(allowlist, prefix):
    message = commands.allow_command(prefix)
    assert config.ALLOWED_COMMANDS == []
    assert not session.allowed_file().exists()
    assert "Not allowed" in message


def test_saved_for_this_project_only(allowlist, tmp_path, monkeypatch):
    commands.allow_command("python -m pytest")
    config.ALLOWED_COMMANDS.clear()
    assert commands.load_saved_allowed() == ["python -m pytest"]
    assert config.ALLOWED_COMMANDS == ["python -m pytest"]

    config.ALLOWED_COMMANDS.clear()
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    assert commands.load_saved_allowed() == []
    assert config.ALLOWED_COMMANDS == []


def test_saved_outside_the_project(allowlist):
    # A cloned repo must not be able to ship an allowlist.
    commands.allow_command("echo")
    assert allowlist not in session.allowed_file().parents
    assert session.allowed_file().parent == config.SESSION_DIR


def test_a_file_in_the_repo_grants_nothing(allowlist):
    (allowlist / ".llm_light").mkdir()
    (allowlist / ".llm_light" / "allowed.json").write_text('["rm"]', encoding="utf-8")
    assert commands.load_saved_allowed() == []


def test_load_does_not_duplicate_a_flag_entry(allowlist):
    commands.allow_command("echo")
    assert commands.load_saved_allowed() == []  # already in the list from this session
    assert config.ALLOWED_COMMANDS == ["echo"]


@pytest.mark.parametrize("content, kept", [
    ("not json", []),
    ('{"echo": 1}', []),
    ('[1, null, ["echo"]]', []),
    ('["echo", "rm x; curl y", "git *", "python -m pytest"]', ["echo", "python -m pytest"]),
])
def test_a_damaged_or_edited_saved_file_grants_only_valid_entries(allowlist, content, kept):
    session.allowed_file().parent.mkdir(parents=True)
    session.allowed_file().write_text(content, encoding="utf-8")
    commands.load_saved_allowed()
    assert config.ALLOWED_COMMANDS == kept


def test_disallow_removes_it_now_and_for_later_sessions(allowlist):
    commands.allow_command("echo")
    commands.allow_command("python -m pytest")
    assert "echo" in commands.disallow_command("echo")
    assert run_tool("shell", {"command": "echo hi"}, root=allowlist)[1] is True
    assert json.loads(session.allowed_file().read_text(encoding="utf-8")) == ["python -m pytest"]


def test_disallowing_the_last_removes_the_file(allowlist):
    commands.allow_command("echo")
    commands.disallow_command("echo")
    assert not session.allowed_file().exists()


def test_disallow_unknown_prefix_says_so(allowlist):
    commands.allow_command("echo")
    assert "not allowed" in commands.disallow_command("nope").lower()
    assert config.ALLOWED_COMMANDS == ["echo"]


def test_disallow_also_covers_a_flag_entry_for_this_session(allowlist):
    config.ALLOWED_COMMANDS.append("echo")  # from --allow-command
    commands.disallow_command("echo")
    assert config.ALLOWED_COMMANDS == []


def test_allow_without_arguments_lists_them(allowlist):
    assert "/allow <prefix>" in commands.allow_command("")
    commands.allow_command("echo")
    assert "echo" in commands.allow_command("")


def test_allow_says_when_the_mode_overrides_the_list(allowlist):
    permissions.set_mode(permissions.PLAN)
    assert "plan" in commands.allow_command("echo").lower()
    state.bypass_allowed = True
    permissions.set_mode(permissions.BYPASS)
    assert "bypass" in commands.allow_command("ls").lower()


def test_status_points_at_allow_when_the_shell_is_off(allowlist):
    assert any("/allow" in line for line in commands.status_lines())


def test_listed_in_help(allowlist):
    names = {c.split()[0] for c, _ in main._all_commands()}
    assert {"/allow", "/disallow"} <= names


def test_help_shows_the_arguments_of_every_command(capsys):
    # Rich reads [prefix] as a markup tag and drops it, so /help showed a bare /allow.
    main._print_help()
    out = capsys.readouterr().out
    for shown in ("/allow [prefix]", "/copy [n]", "/export [file]", "/model [name]",
                  "/permissions [mode]", "/loop <n|interval> <question>"):
        assert shown in out
