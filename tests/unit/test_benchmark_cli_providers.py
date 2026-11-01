"""Tests for tests/benchmarks/benchmark_cli_providers.py.

The benchmark is live (it starts the real codex CLI), so these tests load it by
path, patch run_cli, and check what it reports: the usage codex prints, the
number of agent messages, and that --baseline runs the untrimmed argv through
the same CodexProvider._run.
"""
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from llm_light.providers import codex

_BENCH = Path(__file__).resolve().parents[1] / "benchmarks" / "benchmark_cli_providers.py"


@pytest.fixture
def bench():
    spec = importlib.util.spec_from_file_location("benchmark_cli_providers", _BENCH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stdout(*texts, input_tokens=100, output_tokens=7):
    lines = [json.dumps({"type": "item.completed",
                         "item": {"type": "agent_message", "text": t}}) for t in texts]
    lines.append(json.dumps({"type": "turn.completed", "usage": {
        "input_tokens": input_tokens, "cached_input_tokens": 40,
        "output_tokens": output_tokens, "reasoning_output_tokens": 3}}))
    return "\n".join(lines) + "\n"


def _run_once(bench, stdout, baseline=False):
    seen = {}

    def fake(provider, argv, stdin_text, *a, **kw):
        seen["argv"] = argv
        seen["stdin"] = stdin_text
        return stdout
    with patch("shutil.which", return_value="/bin/codex"), \
         patch.object(codex, "run_cli", side_effect=fake):
        row = bench.run_codex("the prompt", baseline=baseline)
    return row, seen


def test_run_reports_codex_usage_and_agent_messages(bench):
    row, _ = _run_once(bench, _stdout("Let me look.", "the answer",
                                      input_tokens=26_850, output_tokens=1_128))
    assert row == {"input_tokens": 26_850, "cached_input_tokens": 40,
                   "output_tokens": 1_128, "reasoning_output_tokens": 3,
                   "agent_messages": 2}


def test_run_goes_through_the_shipped_run_path_with_the_trimmed_argv(bench):
    _, seen = _run_once(bench, _stdout("a"))
    assert seen["stdin"] == "the prompt"
    assert any(v.startswith("model_instructions_file=") for v in seen["argv"])


def test_baseline_runs_the_untrimmed_argv(bench):
    _, seen = _run_once(bench, _stdout("a"), baseline=True)
    assert seen["argv"] == ["/bin/codex"] + codex._ARGV_TAIL


def test_baseline_does_not_leak_into_the_next_run(bench):
    _run_once(bench, _stdout("a"), baseline=True)
    _, seen = _run_once(bench, _stdout("a"))
    assert seen["argv"] != ["/bin/codex"] + codex._ARGV_TAIL


def test_prompt_is_llm_lights_transcript_of_system_prompt_files_and_question(bench):
    from llm_light.config import SYSTEM_PROMPT
    prompt = bench.build_prompt(["llm_light/providers/runner.py"], "What does run_cli do?")
    assert prompt.startswith(SYSTEM_PROMPT)
    assert "def run_cli(" in prompt
    assert prompt.endswith("Question:\nWhat does run_cli do?")


def test_summary_is_the_mean_of_each_figure(bench):
    rows = [{"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 4,
             "reasoning_output_tokens": 0, "agent_messages": 1},
            {"input_tokens": 30, "cached_input_tokens": 2, "output_tokens": 6,
             "reasoning_output_tokens": 2, "agent_messages": 3}]
    assert bench.mean(rows) == {"input_tokens": 20, "cached_input_tokens": 1,
                                "output_tokens": 5, "reasoning_output_tokens": 1,
                                "agent_messages": 2}


def test_summary_keeps_a_fraction_of_agent_messages(bench):
    one = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
           "reasoning_output_tokens": 0, "agent_messages": 1}
    rows = [one] * 4 + [dict(one, agent_messages=2)]
    assert bench.mean(rows)["agent_messages"] == 1.2


# --- agy (#149) -------------------------------------------------------------

from llm_light.providers import gemini  # noqa: E402


def _agy_stdout(*tools, input_tokens=1_249, output_tokens=515, response="the answer"):
    lines = [json.dumps({"event": "init"})]
    for t in tools:
        lines.append(json.dumps({"event": "step_update", "step_update": {
            "step_type": "tool", "tool_name": t, "state": "DONE"}}))
    lines.append(json.dumps({"event": "result", "result": {
        "status": "SUCCESS", "response": response, "num_turns": 1,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens,
                  "thinking_tokens": 486, "cache_read_tokens": 0}}}))
    return "\n".join(lines) + "\n"


def _run_agy(bench, stdout, baseline=False):
    import os
    seen = {}

    def fake(provider, argv, stdin_text, *a, **kw):
        home = kw["env"]["HOME"]
        seen["argv"] = argv
        seen["stdin"] = stdin_text
        seen["agent"] = os.path.exists(os.path.join(home, *gemini._AGENT_FILE))
        return stdout
    with patch("shutil.which", return_value="/bin/agy"), \
         patch.object(gemini, "run_cli", side_effect=fake):
        row = bench.run_gemini("the prompt", baseline=baseline)
    return row, seen


def test_agy_run_reports_the_usage_agy_prints_and_its_tool_steps(bench):
    row, _ = _run_agy(bench, _agy_stdout())
    assert row == {"input_tokens": 1_249, "output_tokens": 515, "thinking_tokens": 486,
                   "cache_read_tokens": 0, "tool_steps": 0, "error": 0}


def test_agy_run_goes_through_the_shipped_run_path_with_the_agent(bench):
    _, seen = _run_agy(bench, _agy_stdout())
    assert seen["agent"] is True
    assert seen["argv"][seen["argv"].index("--agent") + 1] == gemini.AGENT_NAME
    assert json.loads(seen["stdin"])["message"]["content"].endswith("the prompt")


def test_agy_baseline_runs_agys_default_agent(bench):
    _, seen = _run_agy(bench, _agy_stdout(), baseline=True)
    assert seen["agent"] is False
    assert "--agent" not in seen["argv"]


def test_agy_baseline_counts_a_search_the_shipped_path_refuses(bench):
    # The default agent may run search_web; _run raises on it (fail closed),
    # but its usage is still what that run cost.
    row, _ = _run_agy(bench, _agy_stdout("search_web", "search_web", input_tokens=34_648),
                      baseline=True)
    assert row["input_tokens"] == 34_648 and row["tool_steps"] == 2 and row["error"] == 1


def test_agy_baseline_does_not_leak_into_the_next_run(bench):
    _run_agy(bench, _agy_stdout(), baseline=True)
    _, seen = _run_agy(bench, _agy_stdout())
    assert "--agent" in seen["argv"] and seen["agent"] is True


def test_codex_effort_reaches_the_shipped_argv(bench):
    seen = {}

    def fake(provider, argv, stdin_text, *a, **kw):
        seen["argv"] = argv
        return _stdout("a")
    with patch("shutil.which", return_value="/bin/codex"), \
         patch.object(codex, "run_cli", side_effect=fake):
        bench.run_codex("the prompt", effort="low")
    assert "model_reasoning_effort=low" in seen["argv"]
