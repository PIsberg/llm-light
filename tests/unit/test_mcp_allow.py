"""A server's "allow" list in mcp.json: only the tools it names may run (#220).

Without the list every tool of a listed server runs unasked, so one write tool among
them (post a comment, push, delete a row) could be called by a turn steered by repo text.
"""

import io
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from llm_light import config, mcp_servers, permissions
from llm_light.providers import claude as _claude_mod


@pytest.fixture
def mcp_file(tmp_path, monkeypatch):
    path = tmp_path / "home" / "mcp.json"
    monkeypatch.setenv("LLM_LIGHT_MCP_CONFIG", str(path))
    mcp_servers.reset()

    def write(data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        mcp_servers.reset()
        return path
    yield write
    mcp_servers.reset()


@pytest.fixture
def accept_edits(monkeypatch):
    permissions.set_mode(permissions.ACCEPT_EDITS)
    monkeypatch.setattr(config, "ALLOWED_COMMANDS", [])
    monkeypatch.setattr(config, "WEB_ACCESS", False)
    yield
    permissions.set_mode(permissions.DEFAULT)


def _servers(mcp_file, **servers):
    mcp_file({"mcpServers": servers})
    return mcp_servers.configured()


# --- the config file ----------------------------------------------------------------------

def test_without_allow_the_entry_is_unchanged(mcp_file):
    servers = _servers(mcp_file, gh={"command": "gh-mcp"})
    assert servers["gh"] == {"type": "stdio", "command": "gh-mcp", "args": [], "env": {}}


def test_allow_is_kept(mcp_file):
    servers = _servers(mcp_file, gh={"command": "gh-mcp", "allow": ["get_issue", "search_*"]},
                       docs={"type": "http", "url": "https://x.test/mcp", "allow": []})
    assert servers["gh"]["allow"] == ["get_issue", "search_*"]
    assert servers["docs"]["allow"] == []
    assert mcp_servers.problems() == []


@pytest.mark.parametrize("allow", [
    "get_issue",              # a string, not a list
    [1],
    [""],
    ["get_issue,Bash"],       # a comma would add a rule of its own to --allowedTools
    ["get(x)"],
    ["get issue"],
    ["x" * 65],
])
def test_a_bad_allow_list_skips_the_server(mcp_file, allow):
    # Skipped, not loaded with every tool allowed: the user meant to narrow it.
    servers = _servers(mcp_file, gh={"command": "gh-mcp", "allow": allow})
    assert servers == {}
    assert "allow" in mcp_servers.problems()[0]


# --- the claude CLI agent -------------------------------------------------------------------

def test_rules_name_only_the_allowed_tools(mcp_file):
    servers = _servers(mcp_file, gh={"command": "gh-mcp", "allow": ["get_issue", "search_*"]},
                       docs={"command": "docs-mcp"},
                       locked={"command": "x", "allow": []})
    assert mcp_servers.allow_rules(servers) == [
        "mcp__gh__get_issue", "mcp__gh__search_*", "mcp__docs"]


def test_the_cli_config_carries_no_allow_key(mcp_file):
    # The CLI reads --mcp-config as its own schema.
    servers = _servers(mcp_file, gh={"command": "gh-mcp", "allow": ["get_issue"]})
    assert "allow" not in mcp_servers.cli_config(servers)["mcpServers"]["gh"]


def test_cli_turn_passes_the_tool_rules(mcp_file, accept_edits):
    mcp_file({"mcpServers": {"gh": {"command": "gh-mcp", "allow": ["get_issue"]}}})
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
        _claude_mod._make_cli_subprocess_call("hi")
    cmd = seen["cmd"]
    assert cmd[cmd.index("--allowedTools") + 1] == "mcp__gh__get_issue"


def test_status_line_says_what_is_allowed(mcp_file):
    servers = _servers(mcp_file, gh={"command": "gh-mcp", "allow": ["get_issue", "search_*"]},
                       docs={"command": "docs-mcp"}, locked={"command": "x", "allow": []})
    line = " ".join(mcp_servers.cli_status_lines(servers))
    assert "gh (get_issue, search_*)" in line
    assert "docs (all tools)" in line
    assert "locked (no tools)" in line


# --- API-key mode ---------------------------------------------------------------------------

def _tool(name):
    return SimpleNamespace(name=name, title=None, description=f"{name} tool",
                           input_schema={"type": "object", "properties": {}})


def _definitions(entry, listed):
    manager = mcp_servers._Manager()
    srv = mcp_servers._Server("gh", {"type": "stdio", "command": "x", "args": [], "env": {}, **entry})
    srv.listed = [_tool(n) for n in listed]
    srv.client = object()
    manager.servers = {"gh": srv}
    manager._build_definitions()
    return manager


def test_api_key_mode_offers_only_the_allowed_tools():
    # Left out of the list, not refused on call: the model never sees them, and their
    # schemas cost nothing.
    manager = _definitions({"allow": ["get_issue", "search_*"]},
                           ["get_issue", "search_code", "search_issues", "create_issue", "Search_x"])
    names = [d["name"] for d in manager.definitions]
    assert names == ["mcp__gh__get_issue", "mcp__gh__search_code", "mcp__gh__search_issues"]
    text, is_error = manager.call("mcp__gh__create_issue", {})
    assert is_error


def test_api_key_mode_without_allow_offers_every_tool():
    manager = _definitions({}, ["get_issue", "create_issue"])
    assert [d["name"] for d in manager.definitions] == ["mcp__gh__get_issue", "mcp__gh__create_issue"]


def test_status_counts_only_the_offered_tools():
    manager = _definitions({"allow": ["get_issue"]}, ["get_issue", "create_issue"])
    line = manager.status_lines()[0]
    assert "1 tools (get_issue)" in line and "1 more not allowed" in line
