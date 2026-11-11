"""--web: the subscription agent may search and fetch web pages in every permission mode.

Without the flag nothing changes: the CLI agent gets no web tool outside bypass mode,
so a turn that reads untrusted repo text has no way to send it to a URL.
"""

import io
from unittest.mock import MagicMock, patch

import pytest

from llm_light import config, main, permissions
from llm_light.providers import claude as _claude_mod

WEB = {"WebSearch", "WebFetch"}


@pytest.fixture
def mode():
    import llm_light.state as state
    allowed = state.bypass_allowed

    def set_mode(name):
        state.bypass_allowed = True  # a startup flag would allow it
        permissions.set_mode(name)
    yield set_mode
    permissions.set_mode(permissions.DEFAULT)
    state.bypass_allowed = allowed


@pytest.fixture
def web(monkeypatch):
    monkeypatch.setattr(config, "WEB_ACCESS", True)
    monkeypatch.setattr(config, "ALLOWED_COMMANDS", [])


@pytest.fixture
def no_web(monkeypatch):
    monkeypatch.setattr(config, "WEB_ACCESS", False)
    monkeypatch.setattr(config, "ALLOWED_COMMANDS", [])


def _cli_run(tools_on=True):
    """The argv of one CLI turn."""
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


def _flag(cmd, name):
    return set(cmd[cmd.index(name) + 1].split(",")) if name in cmd else set()


@pytest.mark.parametrize("name", [permissions.DEFAULT, permissions.ACCEPT_EDITS, permissions.PLAN])
def test_without_web_the_agent_gets_no_web_tool(no_web, mode, name):
    mode(name)
    cmd = _cli_run()
    assert not WEB & _flag(cmd, "--tools")
    assert not WEB & _flag(cmd, "--allowedTools")


@pytest.mark.parametrize("name", [permissions.DEFAULT, permissions.ACCEPT_EDITS, permissions.PLAN])
def test_web_offers_and_allows_both_tools(web, mode, name):
    # Offered but not allowed would be denied in a -p run: there is no one to ask.
    mode(name)
    cmd = _cli_run()
    assert WEB <= _flag(cmd, "--tools")
    assert WEB <= _flag(cmd, "--allowedTools")
    assert not WEB & _flag(cmd, "--disallowed-tools")


def test_web_keeps_the_command_rules(web, mode, monkeypatch):
    mode(permissions.ACCEPT_EDITS)
    monkeypatch.setattr(config, "ALLOWED_COMMANDS", ["python -m pytest"])
    cmd = _cli_run()
    assert _flag(cmd, "--allowedTools") == {"Bash(python -m pytest:*)", "WebSearch", "WebFetch"}


def test_plan_mode_with_web_still_writes_nothing(web, mode):
    mode(permissions.PLAN)
    cmd = _cli_run()
    assert _flag(cmd, "--tools") == {"Read", "Grep", "Glob", "WebSearch", "WebFetch"}
    assert {"Edit", "Write", "Bash", "PowerShell"} <= _flag(cmd, "--disallowed-tools")


def test_bypass_needs_no_rule_for_web(web, mode):
    mode(permissions.BYPASS)
    cmd = _cli_run()
    assert WEB <= _flag(cmd, "--tools")
    assert "--allowedTools" not in cmd


def test_hidden_call_never_gets_the_web(web):
    # The history summary reads text that may quote the repo: no tools of any kind.
    cmd = _cli_run(tools_on=False)
    assert not WEB & _flag(cmd, "--tools")
    assert WEB <= _flag(cmd, "--disallowed-tools")
    assert "--allowedTools" not in cmd


def test_flag_is_parsed():
    parser = main._build_parser()
    assert parser.parse_known_args(["--web"])[0].web is True
    assert parser.parse_known_args([])[0].web is False


@pytest.mark.parametrize("value, expected", [
    ("1", True), ("true", True), ("on", True), ("yes", True),
    ("", False), ("0", False), ("off", False), ("no", False),
])
def test_env_var(value, expected):
    assert config.parse_flag(value) is expected


@pytest.mark.parametrize("auth_mode, provider, available", [
    ("OAUTH", "claude", True),
    ("API_KEY", "claude", True),  # Anthropic's server-run web tools (#231)
    ("OAUTH", "codex", False),
    ("OAUTH", "gemini", False),
])
def test_web_note_names_where_it_does_not_work(no_web, monkeypatch, auth_mode, provider, available):
    from llm_light import providers
    monkeypatch.setattr(config, "WEB_ACCESS", True)
    monkeypatch.setattr(main, "AUTH_MODE", auth_mode)
    note = main._web_note(providers.get(provider))
    assert (note is None) is available
    if note:
        assert "--web" in note


def test_no_note_without_the_flag(no_web):
    from llm_light import providers
    assert main._web_note(providers.get("codex")) is None
