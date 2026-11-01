"""
Tests for providers that report no tokens or cost
(specs/001-gemini-codex-providers: T009).

Covers FR-008 (caching figures), SC-005 and the "no cost or token figures"
edge case: usage is None, nothing is added to the session totals, nothing is
saved to the global stats, and the user sees
"tokens, cost and caching unavailable for <provider>" instead of figures.
"""

import copy
import io
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import llm_light  # noqa: F401
from llm_light import config, llm, state, ui
from llm_light import main as main_mod
from llm_light.providers import base

MESSAGE = "tokens, cost and caching unavailable for nofigures"


class NoFiguresProvider(base.Provider):
    """No capabilities; complete() and summarize() report no usage."""
    name = "nofigures"
    capabilities = frozenset()

    def has(self, capability):
        return capability in self.capabilities

    def complete(self, request):
        return "fake answer", None, False

    def summarize(self, instructions):
        return "a summary", None


def _totals():
    with state.lock:
        return (state.session_cost, copy.deepcopy(state.session_tokens),
                copy.deepcopy(state.session_costs))


@pytest.fixture
def no_figures(monkeypatch):
    """Active provider without usage figures, quiet routing, stats file watched."""
    monkeypatch.setattr(state, "provider", NoFiguresProvider(), raising=False)
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
    save = MagicMock()
    monkeypatch.setattr(state, "save_global_stats", save)
    monkeypatch.setattr(llm, "retrieve", lambda *a, **k: ("CHUNK", []))
    monkeypatch.setattr(llm, "route_query", lambda q: ("some-model", "medium", 4096))
    monkeypatch.setattr(llm, "_finalize_route", lambda m, e, t: (m, e, t))
    monkeypatch.setattr(llm, "record_retrieval", lambda *a, **k: None)
    monkeypatch.setattr(llm, "_print_reply", lambda *a, **k: None)
    monkeypatch.setattr(config, "LLMLINGUA_ENABLED", False)
    return save


def _long_history(turns):
    history = []
    for i in range(turns):
        history.append({"role": "user", "content": f"q{i}"})
        history.append({"role": "assistant", "content": f"a{i}"})
    return history


def test_fr008_chat_turn_adds_no_figures_and_says_unavailable(monkeypatch, no_figures):
    monkeypatch.setattr(llm, "_maybe_compress_history", lambda: None)
    stats = MagicMock()
    monkeypatch.setattr(llm, "print_stats", stats)
    before = _totals()
    out = io.StringIO()
    with redirect_stdout(out):
        llm.chat("What does OrderService do?")
    assert _totals() == before
    no_figures.assert_not_called()
    stats.assert_not_called()
    assert state.turn_count == 1
    assert MESSAGE in out.getvalue()


def test_fr008_one_shot_turn_adds_no_figures_and_says_unavailable(monkeypatch, no_figures):
    stats = MagicMock()
    monkeypatch.setattr(llm, "print_stats", stats)
    before = _totals()
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        llm._one_shot_turn("What does OrderService do?", False, True)
    assert _totals() == before
    no_figures.assert_not_called()
    stats.assert_not_called()
    assert MESSAGE in out.getvalue() + err.getvalue()


def test_fr008_compact_history_adds_no_figures(monkeypatch, no_figures):
    state.conversation_history[:] = _long_history(2)
    before = _totals()
    llm.compact_history()
    assert _totals() == before
    no_figures.assert_not_called()
    assert "[Summary of earlier conversation]" in state.conversation_history[0]["content"]


def test_fr008_history_compression_adds_no_figures(monkeypatch, no_figures):
    state.conversation_history[:] = _long_history(config.MAX_HISTORY_TURNS + 3)
    before = _totals()
    with redirect_stdout(io.StringIO()):
        llm._maybe_compress_history()
    assert _totals() == before
    no_figures.assert_not_called()
    assert "[Summary of earlier conversation]" in state.conversation_history[0]["content"]


def test_sc005_status_bar_says_unavailable_with_no_cached_percent_or_dollars(monkeypatch, no_figures):
    state.indexing_done.set()
    text = main_mod._status_text(200)
    assert MESSAGE in text
    assert "% cached" not in text
    assert "$" not in text


def test_sc005_session_summary_says_unavailable(monkeypatch, no_figures):
    out = io.StringIO()
    with redirect_stdout(out):
        ui.print_session_summary()
    assert MESSAGE in out.getvalue()


def test_sc005_exit_footer_says_unavailable_even_with_zero_tokens(monkeypatch, no_figures):
    state.turn_count = 2
    out = io.StringIO()
    with redirect_stdout(out):
        ui.print_session_footer()
    text = out.getvalue()
    assert MESSAGE in text
    assert "$" not in text


def test_fr008_usage_unavailable_text_matches_the_contract():
    assert ui.usage_unavailable("nofigures") == MESSAGE


class LintRetryProvider(NoFiguresProvider):
    """No usage figures; the first reply has a syntax error, the second does not."""
    name = "nofigures"

    def __init__(self):
        self.replies = [
            "Here:\n```python:fixed.py\ndef broken(:\n    pass\n```\n",
            "Here:\n```python:fixed.py\ndef fixed():\n    pass\n```\n",
        ]
        self.requests = []

    def complete(self, request):
        self.requests.append(len(request["messages"]))
        return self.replies.pop(0), None, False


@pytest.mark.parametrize("turn", [
    lambda: llm.chat("Add a function", auto_apply=True),
    lambda: llm._one_shot_turn("Add a function", True, True),
], ids=["chat", "one_shot"])
def test_lint_retry_under_a_provider_without_usage_commits_once(monkeypatch, tmp_path, no_figures, turn):
    """#112: the lint-retry branch records usage before asking for a fix.
    Under a provider that reports none it must not crash, add no figures,
    and still apply and commit the corrected edit exactly once."""
    from llm_light import editor
    provider = LintRetryProvider()
    monkeypatch.setattr(state, "provider", provider, raising=False)
    monkeypatch.setattr(llm, "_maybe_compress_history", lambda: None)
    monkeypatch.setattr(llm, "print_stats", MagicMock())
    monkeypatch.chdir(tmp_path)
    commit = MagicMock(return_value=False)
    monkeypatch.setattr(editor.git_manager, "auto_commit", commit)
    monkeypatch.setattr(editor.git_manager, "is_git_repo", lambda: True)
    monkeypatch.setattr(editor, "show_diff", lambda *a, **k: None)
    before = _totals()
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        turn()
    text = out.getvalue() + err.getvalue()
    assert "API call failed" not in text
    assert "Syntax errors" in text
    assert provider.replies == []
    assert provider.requests[1] == provider.requests[0] + 2  # reply + correction request
    assert (tmp_path / "fixed.py").read_text(encoding="utf-8") == "def fixed():\n    pass\n"
    commit.assert_called_once()
    assert commit.call_args.args[0] == ["fixed.py"]
    assert _totals() == before
    no_figures.assert_not_called()
    assert MESSAGE in text


def test_compression_savings_keep_tokens_but_no_dollars_without_pricing(monkeypatch, no_figures):
    """#114: LLMLingua savings were priced at Claude's rates for every
    provider, so 'Dollars Saved' grew under Gemini and Codex, which have no
    dollar pricing. Token counts stay; the dollar figure does not move."""
    gs = dict(state.global_stats)
    gs.update(total_tokens_pre_compress=0, total_tokens_post_compress=0,
              total_dollars_saved_llmlingua=0.0)
    monkeypatch.setattr(state, "global_stats", gs)
    llm._accumulate_compression_stats(
        {"tokens_before": 10_000, "tokens_after": 4_000}, "some-model")
    assert gs["total_tokens_pre_compress"] == 10_000
    assert gs["total_tokens_post_compress"] == 4_000
    assert state.session_tokens["compress_pre"] == 10_000
    assert state.session_tokens["compress_post"] == 4_000
    assert gs["total_dollars_saved_llmlingua"] == 0.0
