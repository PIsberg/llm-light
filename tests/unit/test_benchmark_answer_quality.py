"""Tests for tests/benchmarks/benchmark_answer_quality.py.

The benchmark is live: it answers repo questions through the claude CLI on the
subscription and has a judge model compare the answers. These tests load it by
path and patch the CLI call, and check the parts a wrong number would come
from: which questions each experiment pairs, how a swapped-order judgement is
combined, that the answer goes through the shipped CLI transport with tools
off, and that it refuses to run in API-key mode, where every call is billed.
"""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from llm_light import config
from llm_light.providers import claude

_BENCH = Path(__file__).resolve().parents[1] / "benchmarks" / "benchmark_answer_quality.py"


@pytest.fixture
def bench():
    spec = importlib.util.spec_from_file_location("benchmark_answer_quality", _BENCH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- judge verdicts ---------------------------------------------------------

def test_parse_verdict_reads_the_json_object_in_the_reply(bench):
    text = 'Both are close.\n```json\n{"winner": "B", "score_a": 1, "score_b": 2}\n```'
    assert bench.parse_verdict(text) == {"winner": "B", "score_a": 1, "score_b": 2}


def test_parse_verdict_takes_the_last_object_when_the_judge_quotes_one(bench):
    text = ('The answer shows {"winner": "A"} in a code sample.\n'
            '{"winner": "tie", "score_a": 2, "score_b": 2}')
    assert bench.parse_verdict(text)["winner"] == "tie"


@pytest.mark.parametrize("text", ["no json here", '{"winner": "C", "score_a": 1, "score_b": 1}',
                                  '{"score_a": 1, "score_b": 2}'])
def test_parse_verdict_rejects_a_reply_without_a_valid_winner(bench, text):
    assert bench.parse_verdict(text) is None


@pytest.mark.parametrize("first, second, expected", [
    ("A", "B", "x"),      # x shown first won, x shown second won
    ("B", "A", "y"),
    ("A", "A", "tie"),    # the judge picked whichever came first: position bias, no winner
    ("tie", "B", "tie"),
    ("tie", "tie", "tie"),
])
def test_combine_counts_a_win_only_when_both_orders_agree(bench, first, second, expected):
    assert bench.combine({"winner": first}, {"winner": second}) == expected


def test_combine_reports_invalid_when_a_judgement_did_not_parse(bench):
    assert bench.combine(None, {"winner": "A"}) == "invalid"


# --- experiment pairs -------------------------------------------------------

_QUESTIONS = [
    {"question": "q-haiku-low", "file": "a.py", "kind": "identifier"},
    {"question": "q-sonnet-medium", "file": "b.py", "kind": "identifier"},
    {"question": "q-sonnet-high", "file": "c.py", "kind": "paraphrase"},
    {"question": "q-unrelated", "kind": "unrelated"},
]
_ROUTES = {
    "q-haiku-low": (config.MODEL_HAIKU, "low", 1024),
    "q-sonnet-medium": (config.MODEL_SONNET, "medium", 2048),
    "q-sonnet-high": (config.MODEL_SONNET, "high", 4096),
    "q-unrelated": (config.MODEL_HAIKU, "low", 1024),
}


def _route(q):
    return _ROUTES[q]


def test_models_experiment_pairs_haiku_with_sonnet_on_sonnet_routed_questions(bench):
    pairs = bench.pairs("models", _QUESTIONS, _route)
    assert [p["question"]["question"] for p in pairs] == ["q-sonnet-medium", "q-sonnet-high"]
    medium = pairs[0]
    assert medium["x"]["model"] == config.MODEL_HAIKU and medium["y"]["model"] == config.MODEL_SONNET
    # Same retrieval on both sides: only the model differs.
    assert medium["x"]["budget"] == medium["y"]["budget"] == config._RETRIEVAL_BUDGET["medium"]


def test_budget_experiment_compares_medium_and_high_budget_on_high_routed_questions(bench):
    pairs = bench.pairs("budget", _QUESTIONS, _route)
    assert [p["question"]["question"] for p in pairs] == ["q-sonnet-high"]
    x, y = pairs[0]["x"], pairs[0]["y"]
    assert (x["budget"], y["budget"]) == (config._RETRIEVAL_BUDGET["medium"],
                                          config._RETRIEVAL_BUDGET["high"])
    # The render effort stays high on both sides (two detailed chunks), so
    # only the number of chunks selected differs.
    assert x["effort"] == y["effort"] == "high"
    assert x["model"] == y["model"] == config.MODEL_SONNET


# --- the live call ----------------------------------------------------------

def test_answer_goes_through_the_cli_transport_with_tools_off(bench):
    seen = {}

    def fake_cli(prompt, model="", extra_flags=None, render=True, tools=True):
        seen.update(prompt=prompt, model=model, flags=extra_flags, render=render, tools=tools)
        return "the answer", SimpleNamespace(input_tokens=10, output_tokens=5,
                                             cache_read_input_tokens=0,
                                             cache_creation_input_tokens=0), "sid"

    with patch.object(claude, "_make_cli_subprocess_call", side_effect=fake_cli):
        row = bench.answer("SKELETON-TEXT", "RETRIEVED-CHUNKS", "How does X work?",
                           config.MODEL_SONNET, "medium")

    assert row["text"] == "the answer" and row["output_tokens"] == 5
    assert seen["tools"] is False and seen["render"] is False
    assert seen["model"] == config.MODEL_SONNET
    system = seen["flags"][seen["flags"].index("--system-prompt") + 1]
    assert "SKELETON-TEXT" in system and config.SYSTEM_PROMPT in system
    assert "RETRIEVED-CHUNKS" in seen["prompt"] and "How does X work?" in seen["prompt"]


def test_judge_sees_the_reference_and_both_answers_in_both_orders(bench):
    prompts = []

    def fake_cli(prompt, model="", extra_flags=None, render=True, tools=True):
        prompts.append(prompt)
        assert tools is False
        return json.dumps({"winner": "A", "score_a": 2, "score_b": 1}), None, None

    with patch.object(claude, "_make_cli_subprocess_call", side_effect=fake_cli):
        outcome, verdicts = bench.judge("Q?", "REFERENCE", "ANSWER-X", "ANSWER-Y")

    assert len(prompts) == 2
    assert prompts[0].index("ANSWER-X") < prompts[0].index("ANSWER-Y")
    assert prompts[1].index("ANSWER-Y") < prompts[1].index("ANSWER-X")
    assert all("REFERENCE" in p for p in prompts)
    assert outcome == "tie"  # "A" both times: the judge followed position, not content
    assert len(verdicts) == 2


def test_main_refuses_in_api_key_mode(bench, capsys):
    with patch.object(config, "AUTH_MODE", "API_KEY"):
        assert bench.main(["--experiment", "models", "--limit", "1"]) == 2
    assert "OAUTH" in capsys.readouterr().err


# --- report -----------------------------------------------------------------

def test_summarize_counts_outcomes_and_mean_tokens_per_side(bench):
    rows = [
        {"experiment": "models", "outcome": "y", "x": {"output_tokens": 100, "input_tokens": 1000},
         "y": {"output_tokens": 300, "input_tokens": 1000}},
        {"experiment": "models", "outcome": "tie", "x": {"output_tokens": 200, "input_tokens": 1000},
         "y": {"output_tokens": 100, "input_tokens": 1000}},
    ]
    s = bench.summarize(rows)["models"]
    assert (s["x_wins"], s["y_wins"], s["ties"], s["invalid"]) == (0, 1, 1, 0)
    assert s["x_output_tokens"] == 150 and s["y_output_tokens"] == 200


# --- tools (#79): the cost of the agent reading files ----------------------------

def test_tools_experiment_pairs_tools_off_with_tools_on_on_sonnet_routed_questions(bench):
    pairs = bench.pairs("tools", _QUESTIONS, _route)
    assert [p["question"]["question"] for p in pairs] == ["q-sonnet-medium", "q-sonnet-high"]
    x, y = pairs[0]["x"], pairs[0]["y"]
    assert (x["tools"], y["tools"]) == (False, True)
    assert x["model"] == y["model"] == config.MODEL_SONNET
    assert x["budget"] == y["budget"]


def test_tools_on_runs_read_only_in_plan_mode_and_restores_the_mode(bench):
    from llm_light import permissions
    seen = {}

    def fake_cli(prompt, model="", extra_flags=None, render=True, tools=True):
        seen["tools"] = tools
        seen["mode"] = permissions.mode()
        return "answer", None, None

    permissions.set_mode("acceptEdits")
    with patch.object(claude, "_make_cli_subprocess_call", side_effect=fake_cli):
        bench.answer("SK", "CTX", "Q?", config.MODEL_SONNET, "medium", tools=True)
    assert seen == {"tools": True, "mode": "plan"}  # never acceptEdits with the repo as cwd
    assert permissions.mode() == "acceptEdits"


def test_tools_off_is_unchanged(bench):
    seen = {}

    def fake_cli(prompt, model="", extra_flags=None, render=True, tools=True):
        seen["tools"] = tools
        return "answer", None, None

    with patch.object(claude, "_make_cli_subprocess_call", side_effect=fake_cli):
        bench.answer("SK", "CTX", "Q?", config.MODEL_SONNET, "medium")
    assert seen["tools"] is False


def test_summarize_reports_total_input_including_cache(bench):
    side = lambda i, r, w: {"output_tokens": 0, "input_tokens": i, "cache_read_input_tokens": r,
                            "cache_creation_input_tokens": w}
    rows = [{"experiment": "tools", "outcome": "tie", "x": side(10, 1000, 0),
             "y": side(20, 5000, 3000)}]
    s = bench.summarize(rows)["tools"]
    assert (s["x_total_input_tokens"], s["y_total_input_tokens"]) == (1010, 8020)
