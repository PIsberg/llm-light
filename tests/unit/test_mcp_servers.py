"""MCP servers from ~/.llm_light/mcp.json: loading, the CLI agent's flags, tool names,
result conversion and the API-key tool list. No server is started here; the real
stdio round trips are in tests/integration/test_mcp_stdio.py."""

import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from llm_light import config, mcp_servers, permissions, tools
from llm_light.providers import claude as _claude_mod


@pytest.fixture
def mcp_file(tmp_path, monkeypatch):
    """Point LLM_LIGHT_MCP_CONFIG at a temp file; returns a writer for it."""
    path = tmp_path / "home" / "mcp.json"
    monkeypatch.setenv("LLM_LIGHT_MCP_CONFIG", str(path))
    mcp_servers.reset()

    def write(data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        mcp_servers.reset()
        return path
    yield write
    mcp_servers.reset()


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


# --- loading ----------------------------------------------------------------------------

def test_no_file_means_no_servers_and_no_problems(mcp_file):
    assert mcp_servers.load() == ({}, [])


def test_stdio_and_http_entries_are_normalised(mcp_file):
    mcp_file({"mcpServers": {
        "local": {"command": "python", "args": ["srv.py", "--x"], "env": {"TOKEN": "t"}},
        "remote": {"type": "http", "url": "https://example.test/mcp", "headers": {"A": "b"}},
        "events": {"type": "sse", "url": "https://example.test/sse"},
    }})
    servers, problems = mcp_servers.load()
    assert problems == []
    assert servers == {
        "local": {"type": "stdio", "command": "python", "args": ["srv.py", "--x"], "env": {"TOKEN": "t"}},
        "remote": {"type": "http", "url": "https://example.test/mcp", "headers": {"A": "b"}},
        "events": {"type": "sse", "url": "https://example.test/sse", "headers": {}},
    }


def test_a_broken_file_loads_nothing_and_says_why(mcp_file):
    path = mcp_file("{not json")
    servers, problems = mcp_servers.load()
    assert servers == {}
    assert len(problems) == 1 and str(path) in problems[0]


@pytest.mark.parametrize("name", ["bad name", "a,b", "x*", "mcp(y)", "", "a" * 33, "dots.no"])
def test_a_server_name_that_cannot_be_a_tool_rule_is_skipped(mcp_file, name):
    # The name becomes the mcp__<name> allow rule and part of every tool name, so a
    # comma, bracket or star would change the rule's meaning.
    mcp_file({"mcpServers": {name: {"command": "python"}, "ok": {"command": "python"}}})
    servers, problems = mcp_servers.load()
    assert list(servers) == ["ok"]
    assert len(problems) == 1


@pytest.mark.parametrize("entry", [
    {},
    {"command": ""},
    {"command": "python", "args": "not-a-list"},
    {"command": "python", "env": {"A": 1}},
    {"type": "http"},
    {"type": "http", "url": "ftp://example.test"},
    {"type": "websocket", "url": "wss://example.test"},
    {"type": "http", "url": "https://example.test", "headers": ["A: b"]},
    "python srv.py",
])
def test_an_invalid_entry_is_skipped_with_a_problem(mcp_file, entry):
    mcp_file({"mcpServers": {"broken": entry}})
    servers, problems = mcp_servers.load()
    assert servers == {}
    assert len(problems) == 1 and "broken" in problems[0]


def test_a_project_mcp_json_is_never_read(mcp_file, tmp_path, monkeypatch):
    # A cloned repo controls its own .mcp.json; only the home-directory file counts.
    project = tmp_path / "project"
    project.mkdir()
    (project / ".mcp.json").write_text(json.dumps(
        {"mcpServers": {"planted": {"command": "evil"}}}), encoding="utf-8")
    (project / "mcp.json").write_text(json.dumps(
        {"mcpServers": {"planted": {"command": "evil"}}}), encoding="utf-8")
    monkeypatch.chdir(project)
    assert mcp_servers.load() == ({}, [])


def test_default_file_is_under_the_home_directory(monkeypatch):
    monkeypatch.delenv("LLM_LIGHT_MCP_CONFIG", raising=False)
    assert mcp_servers.config_file() == Path.home() / ".llm_light" / "mcp.json"


def test_env_references_expand_like_the_claude_cli(monkeypatch):
    monkeypatch.setenv("MCP_TEST_TOKEN", "s3cret")
    monkeypatch.delenv("MCP_TEST_UNSET", raising=False)
    assert mcp_servers.expand("Bearer ${MCP_TEST_TOKEN}") == "Bearer s3cret"
    assert mcp_servers.expand("${MCP_TEST_UNSET:-fallback}") == "fallback"
    assert mcp_servers.expand("${MCP_TEST_UNSET}") == ""
    assert mcp_servers.expand("$MCP_TEST_TOKEN") == "$MCP_TEST_TOKEN"


# --- the claude CLI agent (subscription) ------------------------------------------------

def test_cli_config_gives_stdio_servers_the_real_home(mcp_file):
    # The CLI runs with HOME pointed at an empty temp dir; a server it starts would
    # otherwise lose its own config and caches under the user's home.
    servers = {
        "local": {"type": "stdio", "command": "python", "args": [], "env": {}},
        "own": {"type": "stdio", "command": "python", "args": [], "env": {"HOME": "/mine"}},
        "remote": {"type": "http", "url": "https://example.test/mcp", "headers": {}},
    }
    cfg = mcp_servers.cli_config(servers)
    home = os.path.expanduser("~")
    assert cfg["mcpServers"]["local"]["env"]["HOME"] == home
    assert cfg["mcpServers"]["local"]["env"]["USERPROFILE"] == home
    assert cfg["mcpServers"]["own"]["env"]["HOME"] == "/mine"
    assert cfg["mcpServers"]["remote"] == {"type": "http", "url": "https://example.test/mcp", "headers": {}}
    assert servers["local"]["env"] == {}  # the loaded entry is not changed


def _cli_run(tools_on=True):
    """The argv of one CLI turn, plus the --mcp-config file's content read while it runs."""
    seen = {}
    proc = MagicMock()
    proc.stdout = iter(['{"type":"result","subtype":"success","result":"","usage":{}}\n'])
    proc.stderr = io.StringIO("")
    proc.wait.return_value = proc.poll.return_value = proc.returncode = 0

    def popen(cmd, **kwargs):
        seen["cmd"] = cmd
        if "--mcp-config" in cmd:
            seen["mcp"] = json.loads(Path(cmd[cmd.index("--mcp-config") + 1]).read_text(encoding="utf-8"))
        return proc
    with patch("llm_light.llm.subprocess.Popen", side_effect=popen), \
         patch("shutil.which", return_value="/fake/bin/claude"), \
         patch("sys.stdout", io.StringIO()):
        _claude_mod._make_cli_subprocess_call("hi", tools=tools_on)
    return seen["cmd"], seen.get("mcp")


def test_cli_turn_without_servers_is_unchanged(mcp_file):
    cmd, mcp = _cli_run()
    assert "--mcp-config" not in cmd and mcp is None
    assert "--strict-mcp-config" in cmd


def test_cli_turn_gets_the_servers_and_may_call_their_tools(mcp_file, mode):
    # Without the mcp__<server> rule a -p run denies every call (claude 2.1.321:
    # permission_denials named mcp__nonce__nonce); with it the tool ran.
    mcp_file({"mcpServers": {"nonce": {"command": "python", "args": ["srv.py"]},
                             "docs": {"type": "http", "url": "https://example.test/mcp"}}})
    mode(permissions.ACCEPT_EDITS)
    with patch.object(config, "ALLOWED_COMMANDS", ["python -m pytest"]):
        cmd, mcp = _cli_run()
    assert "--strict-mcp-config" in cmd  # the repo's .mcp.json still never loads
    assert set(mcp["mcpServers"]) == {"nonce", "docs"}
    assert mcp["mcpServers"]["nonce"]["command"] == "python"
    rules = cmd[cmd.index("--allowedTools") + 1].split(",")
    assert rules == ["Bash(python -m pytest:*)", "mcp__nonce", "mcp__docs"]


def test_cli_turn_in_plan_mode_has_no_servers(mcp_file, mode):
    # An MCP tool can change things outside the repo, and plan mode changes nothing.
    mcp_file({"mcpServers": {"nonce": {"command": "python"}}})
    mode(permissions.PLAN)
    cmd, mcp = _cli_run()
    assert "--mcp-config" not in cmd and "--allowedTools" not in cmd


def test_cli_turn_in_bypass_mode_needs_no_rules(mcp_file, mode):
    mcp_file({"mcpServers": {"nonce": {"command": "python"}}})
    mode(permissions.BYPASS)
    cmd, mcp = _cli_run()
    assert set(mcp["mcpServers"]) == {"nonce"}
    assert "--allowedTools" not in cmd


def test_hidden_cli_call_has_no_servers(mcp_file):
    # The history summary reads text that may quote the repo: no tools of any kind.
    mcp_file({"mcpServers": {"nonce": {"command": "python"}}})
    cmd, mcp = _cli_run(tools_on=False)
    assert "--mcp-config" not in cmd


# --- tool names and results (API-key mode) ----------------------------------------------

def test_exposed_names_are_valid_api_tool_names():
    assert mcp_servers.exposed_name("gh", "search_issues") == "mcp__gh__search_issues"
    assert mcp_servers.exposed_name("gh", "repo.get/file") == "mcp__gh__repo_get_file"
    assert mcp_servers.exposed_name("gh", "x" * 64) is None  # over the API's 64


def _text(t):
    return SimpleNamespace(type="text", text=t)


def test_result_text_joins_text_and_keeps_the_error_flag():
    ok = SimpleNamespace(content=[_text("a"), _text("b")], is_error=False, structured_content=None)
    bad = SimpleNamespace(content=[_text("boom")], is_error=True, structured_content=None)
    assert mcp_servers.result_text(ok) == ("a\nb", False)
    assert mcp_servers.result_text(bad) == ("boom", True)


def test_result_text_describes_what_it_cannot_send():
    image = SimpleNamespace(type="image", data="AAAA", mime_type="image/png")
    text_res = SimpleNamespace(type="resource", resource=SimpleNamespace(uri="file:///a", text="body"))
    blob_res = SimpleNamespace(type="resource", resource=SimpleNamespace(uri="file:///b", blob="AAAA"))
    link = SimpleNamespace(type="resource_link", uri="file:///c")
    result = SimpleNamespace(content=[image, text_res, blob_res, link], is_error=False, structured_content=None)
    text, _ = mcp_servers.result_text(result)
    assert "[image/png image omitted]" in text
    assert "body" in text
    assert "file:///b" in text and "AAAA" not in text
    assert "file:///c" in text


def test_result_text_falls_back_to_structured_content_and_is_capped():
    structured = SimpleNamespace(content=[], is_error=False, structured_content={"n": 1})
    assert mcp_servers.result_text(structured) == ('{"n": 1}', False)
    huge = SimpleNamespace(content=[_text("x" * 100_000)], is_error=False, structured_content=None)
    text, _ = mcp_servers.result_text(huge)
    assert len(text) < 41_000 and text.endswith("(output truncated)")


# --- the API-key tool list ----------------------------------------------------------------

_FAKE_DEF = {"name": "mcp__nonce__nonce", "description": "d",
             "input_schema": {"type": "object", "properties": {}}}


def test_without_servers_the_tool_list_is_the_built_in_one():
    # Tools render first in the cached prefix: without a config, every request must
    # stay byte-identical to the one sent before MCP support.
    with patch.object(mcp_servers, "tool_definitions", return_value=[]):
        assert tools.all_tools() is tools.TOOLS


def test_server_tools_follow_the_built_in_ones():
    with patch.object(mcp_servers, "tool_definitions", return_value=[_FAKE_DEF]):
        assert tools.all_tools() == tools.TOOLS + [_FAKE_DEF]


def test_run_tool_sends_mcp_calls_to_the_server(mode):
    mode(permissions.ACCEPT_EDITS)
    with patch.object(mcp_servers, "call", return_value=("N0NCE", False)) as call:
        assert tools.run_tool("mcp__nonce__nonce", {"label": "x"}) == ("N0NCE", False)
    call.assert_called_once_with("mcp__nonce__nonce", {"label": "x"})


def test_plan_mode_refuses_mcp_calls(mode):
    mode(permissions.PLAN)
    with patch.object(mcp_servers, "call") as call:
        text, is_error = tools.run_tool("mcp__nonce__nonce", {})
    assert is_error and "plan mode" in text
    call.assert_not_called()


def test_api_tool_loop_and_cache_warm_send_the_same_list():
    # Warming with a different list than the turns send warms nothing they read.
    import llm_light.state as state
    sent = []

    def fake_stream(client, view=None, **kwargs):
        sent.append(kwargs["tools"])
        return "answer", {"message": SimpleNamespace(stop_reason="end_turn", usage={}, content=[])}
    fake_client = MagicMock()
    fake_client.messages.create.side_effect = lambda **kw: sent.append(kw["tools"]) or MagicMock()
    with patch.object(mcp_servers, "tool_definitions", return_value=[_FAKE_DEF]),          patch.object(_claude_mod, "stream_chat_response", side_effect=fake_stream),          patch.object(_claude_mod, "client", fake_client),          patch.object(_claude_mod, "AUTH_MODE", "API_KEY"),          patch.object(_claude_mod, "calculate_cost", return_value=0.0),          patch.object(_claude_mod, "_accumulate_usage"),          patch.object(state, "session_model", "claude-sonnet-5-5"),          patch("sys.stdout", io.StringIO()):
        _claude_mod._api_tool_loop({"model": "m", "messages": [{"role": "user", "content": "q"}]})
        _claude_mod.ClaudeProvider().warm(quiet=True)
    assert sent == [tools.TOOLS + [_FAKE_DEF]] * 2


def test_call_without_a_started_server_is_an_error():
    mcp_servers.stop()
    text, is_error = mcp_servers.call("mcp__nowhere__x", {})
    assert is_error and "not connected" in text


def test_start_without_the_mcp_package_says_how_to_install_it(mcp_file):
    servers = {"nonce": {"type": "stdio", "command": "python", "args": [], "env": {}}}
    with patch.dict("sys.modules", {"mcp": None}):
        lines = mcp_servers.start(servers)
    assert any("llm-light-cli[mcp]" in line for line in lines)
    assert mcp_servers.tool_definitions() == []


# --- startup, /mcp and /doctor ----------------------------------------------------------

def test_a_provider_without_tools_names_the_servers_it_ignores(mcp_file):
    from llm_light.providers.gemini import GeminiProvider
    mcp_file({"mcpServers": {"nonce": {"command": "python"}}})
    with patch.object(mcp_servers, "start") as start:
        lines = mcp_servers.startup_lines(GeminiProvider())
    start.assert_not_called()
    assert len(lines) == 1 and "not used" in lines[0] and "--provider claude" in lines[0]


def test_subscription_startup_leaves_the_servers_to_the_agent(mcp_file):
    mcp_file({"mcpServers": {"nonce": {"command": "python"}}})
    with patch.object(config, "AUTH_MODE", "OAUTH"), patch.object(mcp_servers, "start") as start:
        lines = mcp_servers.startup_lines(_claude_mod.ClaudeProvider())
    start.assert_not_called()
    assert lines == ["MCP servers for the agent, which starts them itself each turn: nonce"]


def test_api_key_startup_connects(mcp_file):
    mcp_file({"mcpServers": {"nonce": {"command": "python"}}})
    with patch.object(config, "AUTH_MODE", "API_KEY"), \
         patch.object(mcp_servers, "start", return_value=["MCP nonce: 1 tools"]) as start:
        lines = mcp_servers.startup_lines(_claude_mod.ClaudeProvider())
    assert start.call_args.args[0] == {"nonce": {"type": "stdio", "command": "python", "args": [], "env": {}}}
    assert lines == ["MCP nonce: 1 tools"]


def test_startup_reports_skipped_entries(mcp_file):
    mcp_file({"mcpServers": {"bad name": {"command": "python"}}})
    lines = mcp_servers.startup_lines(_claude_mod.ClaudeProvider())
    assert len(lines) == 1 and "bad name" in lines[0]


def test_mcp_command_without_a_file_says_where_it_goes(mcp_file):
    lines = mcp_servers.command_lines(_claude_mod.ClaudeProvider())
    assert str(mcp_servers.config_file()) in lines[0]


def test_mcp_command_in_plan_mode_says_no_tool_runs(mcp_file, mode):
    mcp_file({"mcpServers": {"nonce": {"command": "python"}}})
    mode(permissions.PLAN)
    with patch.object(config, "AUTH_MODE", "OAUTH"):
        lines = mcp_servers.command_lines(_claude_mod.ClaudeProvider())
    assert "nonce" in lines[0] and any("Plan mode" in line for line in lines)


def test_mcp_is_a_listed_command():
    from llm_light import main
    assert "/mcp" in {cmd.split()[0] for cmd, _ in main._COMMANDS}


def _doctor_mcp_line():
    from llm_light import doctor
    with patch("llm_light.providers.active", return_value=_claude_mod.ClaudeProvider()):
        return [(status, line) for status, line in doctor.checks() if line.startswith("MCP")]


def test_doctor_without_servers_says_where_to_add_them(mcp_file):
    from llm_light.providers.base import NOTE
    [(status, line)] = _doctor_mcp_line()
    assert status == NOTE and str(mcp_servers.config_file()) in line


def test_doctor_names_the_servers_without_starting_them(mcp_file):
    from llm_light.providers.base import OK
    mcp_file({"mcpServers": {"nonce": {"command": "python"}, "docs": {"type": "http", "url": "https://x.test"}}})
    with patch.object(config, "AUTH_MODE", "OAUTH"), patch.object(mcp_servers, "start") as start:
        [(status, line)] = _doctor_mcp_line()
    start.assert_not_called()
    assert status == OK and "nonce" in line and "docs" in line


def test_doctor_flags_a_broken_file(mcp_file):
    from llm_light.providers.base import PROBLEM
    mcp_file("{nope")
    [(status, line)] = _doctor_mcp_line()
    assert status == PROBLEM


def test_doctor_flags_api_key_mode_without_the_package(mcp_file):
    from llm_light.providers.base import PROBLEM
    mcp_file({"mcpServers": {"nonce": {"command": "python"}}})
    with patch.object(config, "AUTH_MODE", "API_KEY"), patch("importlib.util.find_spec", return_value=None):
        [(status, line)] = _doctor_mcp_line()
    assert status == PROBLEM and "llm-light-cli[mcp]" in line
