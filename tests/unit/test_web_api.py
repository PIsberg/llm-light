"""--web with an API key: Anthropic's server-run web search and fetch join llm_light's own
tools (#231). Without --web a request is byte-identical to one sent before: the tool list
renders first in the cached prefix."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from llm_light import config, main, mcp_servers, tools
from llm_light.ui import calculate_cost, cost_breakdown

_FAKE_MCP = {"name": "mcp__s__t", "description": "d", "input_schema": {"type": "object"}}


@pytest.fixture
def web(monkeypatch):
    monkeypatch.setattr(config, "WEB_ACCESS", True)


def test_without_web_the_list_is_tools_itself():
    assert config.WEB_ACCESS is False
    assert tools.all_tools("claude-sonnet-5-5") is tools.TOOLS


@pytest.mark.parametrize("model, search, fetch", [
    ("claude-sonnet-5-5", "web_search_20260209", "web_fetch_20260209"),
    ("claude-opus-5-5", "web_search_20260209", "web_fetch_20260209"),
    ("claude-fable-5-1", "web_search_20260209", "web_fetch_20260209"),
    # The dynamic-filtering versions need a newer model than Haiku.
    ("claude-haiku-5-5", "web_search_20250305", "web_fetch_20250910"),
])
def test_web_adds_the_server_tools_after_the_others(web, model, search, fetch):
    listed = tools.all_tools(model)
    assert listed[:len(tools.TOOLS)] == tools.TOOLS
    added = listed[len(tools.TOOLS):]
    assert [(t["type"], t["name"]) for t in added] == [(search, "web_search"), (fetch, "web_fetch")]
    assert added[0]["max_uses"] == tools.WEB_SEARCH_MAX_USES


def test_mcp_tools_stay_before_the_web_tools(web):
    with patch.object(mcp_servers, "tool_definitions", return_value=[_FAKE_MCP]):
        listed = tools.all_tools("claude-sonnet-5-5")
    assert listed[len(tools.TOOLS)] == _FAKE_MCP
    assert [t["name"] for t in listed[-2:]] == ["web_search", "web_fetch"]


def test_a_server_tool_call_never_reaches_run_tool(web):
    # They run at Anthropic; a client-side call by that name is a model error.
    text, is_error = tools.run_tool("web_search", {"query": "x"})
    assert is_error and "unknown tool" in text


def _usage(searches):
    return SimpleNamespace(input_tokens=1000, output_tokens=100, cache_creation_input_tokens=0,
                           cache_read_input_tokens=0,
                           server_tool_use=SimpleNamespace(web_search_requests=searches))


def test_searches_are_billed_on_top_of_tokens():
    tokens_only = calculate_cost(_usage(0), "claude-sonnet-5-5")
    assert calculate_cost(_usage(3), "claude-sonnet-5-5") == pytest.approx(
        tokens_only + 3 * config.WEB_SEARCH_PRICE_USD)
    assert cost_breakdown(_usage(3), "claude-sonnet-5-5")["web_search"] == pytest.approx(0.03)


def test_usage_without_searches_has_no_web_category():
    # The /cost table and the stats file see the same categories as before.
    assert "web_search" not in cost_breakdown(_usage(0), "claude-sonnet-5-5")
    plain = SimpleNamespace(input_tokens=1, output_tokens=1)
    assert "web_search" not in cost_breakdown(plain, "claude-sonnet-5-5")


def test_session_cost_counts_the_searches():
    import llm_light.state as state
    from llm_light import llm
    before = dict(state.session_costs)
    try:
        llm._accumulate_usage(_usage(2), "claude-sonnet-5-5")
        assert state.session_costs["web_search"] - before.get("web_search", 0.0) == pytest.approx(0.02)
    finally:
        state.session_costs.clear()
        state.session_costs.update(before)


def test_the_session_table_shows_the_searches_and_counts_them_in_the_total(monkeypatch):
    import io
    import re
    import llm_light.state as state
    from llm_light import ui
    monkeypatch.setattr(state, "session_tokens", dict(state.session_tokens, input=1000, cache_write=0,
                                                      cache_read=0, output=0))
    monkeypatch.setattr(state, "session_costs", dict(state.session_costs, input=0.002, cache_write=0.0,
                                                     cache_read=0.0, output=0.0, web_search=0.03))
    out = io.StringIO()
    with patch("sys.stdout", out):
        ui.print_session_summary()
    text = re.sub(r"\x1b\[[0-9;]*m", "", out.getvalue())
    assert "Web search" in text and "3 searches" in text
    total = next(line for line in text.splitlines() if "TOTAL" in line)
    assert "$0.0320" in total


def test_the_session_table_has_no_search_row_without_searches(monkeypatch):
    import io
    import llm_light.state as state
    from llm_light import ui
    monkeypatch.setattr(state, "session_tokens", dict(state.session_tokens, input=1000))
    monkeypatch.setattr(state, "session_costs", dict(state.session_costs, web_search=0.0))
    out = io.StringIO()
    with patch("sys.stdout", out):
        ui.print_session_summary()
    assert "Web search" not in out.getvalue()
