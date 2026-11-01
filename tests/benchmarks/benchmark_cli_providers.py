#!/usr/bin/env python3
"""
CLI provider prompt size: the tokens codex or agy reports for one llm_light question.

Builds the prompt the way chat() does for a CLI provider (SYSTEM_PROMPT, the
retrieved files, the question, flattened by providers.base.transcript), runs it
through the shipped CodexProvider._run, and reads the usage codex prints on its
`turn.completed` event. run_cli is wrapped only to read that line, so cwd, env,
argv and the instructions file are exactly what a session uses.

--baseline runs the same path with the untrimmed argv (codex._ARGV_TAIL: codex's
own base instructions, skills list, permissions text and every enabled tool
feature), which is what llm_light sent before the codex prompt trim.

agent_messages counts the agent messages in the run. More than one means codex
took extra model rounds, and each round re-sends the whole prompt, which is
where most of the untrimmed input tokens went.

--provider gemini runs the same prompt through the shipped GeminiProvider._run
and reads the usage on agy's `result` event. --baseline there runs agy's
default agent (no llm-light agent file, no --agent), which is what llm_light
sent before #149. tool_steps counts finished tool calls; error is 1 when the
shipped path refused the run (a search_web the baseline agent ran fails closed
since #122), and the row still records what that run cost.

    python tests/benchmarks/benchmark_cli_providers.py --runs 5
    python tests/benchmarks/benchmark_cli_providers.py --runs 5 --baseline
    python tests/benchmarks/benchmark_cli_providers.py --provider gemini --runs 5 [--baseline]

Live: needs `codex login` (or a signed-in agy for --provider gemini) and uses
the subscription or key the CLI is signed in with. Not collected by pytest. Run from the repo root.
"""
import argparse
import contextlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from llm_light.config import SYSTEM_PROMPT  # noqa: E402
from llm_light.providers import codex, gemini  # noqa: E402
from llm_light.providers.base import ProviderError, json_events, transcript  # noqa: E402

FILES = ["llm_light/providers/runner.py", "llm_light/providers/codex.py"]
QUESTION = ("Find every way run_cli can leave an orphaned process or hang, reason about "
            "each path step by step, and propose a fix for each.")
FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens",
          "agent_messages")
GEMINI_FIELDS = ("input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens")
_COUNTS = ("agent_messages", "tool_steps", "error")  # means kept to one decimal


def build_prompt(files, question) -> str:
    """The stdin text CodexProvider.complete() sends for this question."""
    context = "".join(f"# {f}\n{Path(f).read_text(encoding='utf-8')}\n" for f in files)
    request = {
        "system": [{"type": "text", "text": SYSTEM_PROMPT},
                   {"type": "text", "text": "Project files: " + " ".join(files)}],
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": f"Retrieved Codebase Context:\n{context}\n\n"},
            {"type": "text", "text": f"Question:\n{question}"}]}],
    }
    system_text, prompt = transcript(request)
    return system_text + "\n\n" + prompt


@contextlib.contextmanager
def _untrimmed():
    trimmed = codex._argv_tail
    codex._argv_tail = lambda _instructions, _effort=None: list(codex._ARGV_TAIL)
    try:
        yield
    finally:
        codex._argv_tail = trimmed


def run_codex(prompt: str, baseline: bool = False, effort=None) -> dict:
    """One CodexProvider._run call; codex's usage plus the agent message count."""
    seen = {}
    real = codex.run_cli

    def spy(*args, **kwargs):
        seen["out"] = real(*args, **kwargs)
        return seen["out"]

    codex.run_cli = spy
    try:
        with _untrimmed() if baseline else contextlib.nullcontext():
            codex.PROVIDER._run(prompt, effort)
    finally:
        codex.run_cli = real
    row = dict.fromkeys(FIELDS, 0)
    for event in json_events(seen["out"]):
        if event.get("type") == "turn.completed":
            usage = event.get("usage") or {}
            row.update({k: usage.get(k, 0) for k in FIELDS if k != "agent_messages"})
        item = event.get("item")
        if (event.get("type") == "item.completed" and isinstance(item, dict)
                and item.get("type") == "agent_message"):
            row["agent_messages"] += 1
    return row


@contextlib.contextmanager
def _default_agy_agent():
    """agy's own default agent: no llm-light agent file and no --agent (before #149)."""
    tail, install = gemini._argv_tail, gemini._install_agent

    def without_agent(settings):
        argv = tail(settings)
        i = argv.index("--agent")
        return argv[:i] + argv[i + 2:]
    gemini._argv_tail, gemini._install_agent = without_agent, lambda home: None
    try:
        yield
    finally:
        gemini._argv_tail, gemini._install_agent = tail, install


def run_gemini(prompt: str, baseline: bool = False) -> dict:
    """One GeminiProvider._run call; agy's usage, finished tool steps, and whether it was refused."""
    seen = {}
    real = gemini.run_cli

    def spy(*args, **kwargs):
        seen["out"] = real(*args, **kwargs)
        return seen["out"]

    gemini.run_cli = spy
    error = 0
    try:
        with _default_agy_agent() if baseline else contextlib.nullcontext():
            gemini.PROVIDER._run(prompt)
    except ProviderError:
        if "out" not in seen:
            raise
        error = 1
    finally:
        gemini.run_cli = real
    row = dict.fromkeys(GEMINI_FIELDS, 0)
    row.update(tool_steps=0, error=error)
    for event in json_events(seen["out"]):
        step = event.get("step_update") if event.get("event") == "step_update" else None
        if isinstance(step, dict) and step.get("step_type") == "tool" and step.get("state") == "DONE":
            row["tool_steps"] += 1
        result = event.get("result") if event.get("event") == "result" else None
        if isinstance(result, dict) and isinstance(result.get("usage"), dict):
            row.update({k: result["usage"].get(k, 0) for k in GEMINI_FIELDS})
    return row


def mean(rows) -> dict:
    """Token means to the token; counts to one decimal, so 1.2 does not read as 1."""
    return {k: round(sum(r[k] for r in rows) / len(rows), 1 if k in _COUNTS else None)
            for k in rows[0]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--provider", choices=["codex", "gemini"], default="codex")
    parser.add_argument("--baseline", action="store_true",
                        help="codex: the untrimmed argv (codex._ARGV_TAIL); "
                             "gemini: agy's default agent")
    parser.add_argument("--effort", choices=["low", "medium", "high", "max"], default=None,
                        help="codex: run at this model_reasoning_effort (#150); default: "
                             "codex's model default")
    parser.add_argument("--question", default=QUESTION)
    args = parser.parse_args(argv)
    prompt = build_prompt(FILES, args.question)
    run = run_gemini if args.provider == "gemini" else run_codex
    print(f"{args.provider}, {'baseline' if args.baseline else 'as shipped'}, "
          f"effort {args.effort or 'default'}, "
          f"prompt {len(prompt):,} characters, {args.runs} runs")
    rows = []
    for i in range(args.runs):
        rows.append(run(prompt, baseline=args.baseline) if args.provider == "gemini"
                    else run(prompt, baseline=args.baseline, effort=args.effort))
        print(f"  run {i + 1}: {json.dumps(rows[-1])}", flush=True)
    print(f"  mean:  {json.dumps(mean(rows))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
