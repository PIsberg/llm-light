"""
Tests for a provider that reports token counts but has no dollar pricing (#148).

Codex prints exact token counts, but its prices are not Claude's, so it has the
USAGE capability without USD_BUDGET. Its turns add tokens to the session totals
and show them, and never add or show a dollar figure: model_prices() would fall
back to Claude's rates for a model it does not know.
"""

import copy
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import llm_light  # noqa: F401
from llm_light import config, llm, state, ui
from llm_light import main as main_mod
from llm_light.providers import base

UNAVAILABLE = "tokens, cost and caching unavailable"


def _usage(input_tokens=6_000, cached=3_000, output=500):
    return SimpleNamespace(input_tokens=input_tokens, cache_read_input_tokens=cached,
                           cache_creation_input_tokens=0, output_tokens=output)


class TokensOnlyProvider(base.Provider):
    """Reports token counts (USAGE) but no prices (no USD_BUDGET)."""
    name = "tokensonly"
    capabilities = frozenset({base.USAGE})

    def complete(self, request):
        return "fake answer", _usage(), False

    def summarize(self, instructions):
        return "a summary", _usage(input_tokens=800, cached=0, output=100)


@pytest.fixture
def tokens_only(monkeypatch):
    monkeypatch.setattr(state, "provider", TokensOnlyProvider(), raising=False)
    monkeypatch.setattr(state, "conversation_history", [])
    monkeypatch.setattr(state, "turn_count", 0)
    monkeypatch.setattr(state, "skeleton_context", "SKELETON")
    monkeypatch.setattr(state, "session_cost", 0.0)
    monkeypatch.setattr(state, "session_tokens", {
        "input": 0, "cache_write": 0, "cache_read": 0, "output": 0,
        "compress_pre": 0, "compress_post": 0})
    monkeypatch.setattr(state, "session_costs", {
        "input": 0.0, "cache_write": 0.0, "cache_read": 0.0, "output": 0.0,
        "without_cache": 0.0})
    monkeypatch.setattr(state, "global_stats", dict(state.global_stats))
    monkeypatch.setattr(state, "save_global_stats", MagicMock())
    monkeypatch.setattr(llm, "retrieve", lambda *a, **k: ("CHUNK", []))
    monkeypatch.setattr(llm, "route_query", lambda q: ("some-model", "medium", 4096))
    monkeypatch.setattr(llm, "_finalize_route", lambda m, e, t: (m, e, t))
    monkeypatch.setattr(llm, "record_retrieval", lambda *a, **k: None)
    monkeypatch.setattr(llm, "_print_reply", lambda *a, **k: None)
    monkeypatch.setattr(llm, "_maybe_compress_history", lambda: None)
    monkeypatch.setattr(config, "LLMLINGUA_ENABLED", False)


def _chat():
    out = io.StringIO()
    with redirect_stdout(out):
        llm.chat("What does OrderService do?")
    return out.getvalue()


def _dollars():
    with state.lock:
        return state.session_cost, copy.deepcopy(state.session_costs)


def test_chat_turn_adds_tokens_and_no_dollars(tokens_only):
    gs_before = copy.deepcopy(state.global_stats)
    _chat()
    assert state.session_tokens["input"] == 6_000
    assert state.session_tokens["cache_read"] == 3_000
    assert state.session_tokens["output"] == 500
    assert _dollars() == (0.0, {"input": 0.0, "cache_write": 0.0, "cache_read": 0.0,
                                "output": 0.0, "without_cache": 0.0})
    # Lifetime savings are priced at Claude's rates; an unpriced provider leaves them alone.
    assert state.global_stats == gs_before


def test_chat_turn_line_shows_tokens_and_cache_but_no_dollars(tokens_only):
    text = _chat()
    assert "Turn 1" in text
    assert "9,000 tokens" in text          # 6,000 uncached + 3,000 cached
    assert "cached 3,000 (33.3%)" in text
    assert "output 500" in text
    assert "$" not in text
    assert UNAVAILABLE not in text


def test_summary_usage_adds_tokens_and_no_dollars(tokens_only):
    state.conversation_history[:] = [{"role": "user", "content": "q"},
                                     {"role": "assistant", "content": "a"}]
    llm.compact_history()
    assert state.session_tokens["input"] == 800
    assert _dollars()[0] == 0.0


def test_status_bar_shows_tokens_and_cached_percent_but_no_dollars(tokens_only):
    _chat()
    state.indexing_done.set()
    text = main_mod._status_text(200)
    assert "9,000 tok" in text
    assert "33% cached" in text
    assert "$" not in text


def test_session_summary_shows_token_rows_but_no_dollars(tokens_only):
    _chat()
    out = io.StringIO()
    with redirect_stdout(out):
        ui.print_session_summary()
    text = out.getvalue()
    assert "6,000" in text and "3,000" in text and "500" in text
    assert "$" not in text
    assert UNAVAILABLE not in text


def test_exit_footer_shows_tokens_but_no_dollars(tokens_only):
    _chat()
    out = io.StringIO()
    with redirect_stdout(out):
        ui.print_session_footer()
    text = out.getvalue()
    assert "1 turn" in text
    assert "9,500 tokens" in text
    assert "33.3% cached" in text
    assert "$" not in text
