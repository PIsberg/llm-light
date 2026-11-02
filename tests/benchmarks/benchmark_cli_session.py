#!/usr/bin/env python3
"""
A whole subscription session, live: the same questions asked in order through the real
chat(), once continuing one CLI session (resume) and once re-sending the history as one
prompt on every turn (flat, LLM_LIGHT_CLI_RESUME=0).

The other benchmarks ask one question at a time, so they cannot see what a session costs:
the history that a flat turn writes to the cache again, and the files the agent reads
again because a new CLI process remembers none of them.

  * Each turn is chat() in plan mode, pinned to Sonnet 5.5, with llm_light's own retrieval.
    Tokens and cost are state.session_tokens and state.session_cost, what the user is shown,
    so a history summary counts, and a resume is charged its own cost.
  * Each repeat runs both arms, the order alternating, since the first arm pays the cold
    cache writes for the tools and skeleton that the second one reads.
  * With --judge, Opus 5.5 scores the two answers to each turn in both orders, as
    benchmark_answer_quality.py does, against the source of the file that answers it.

    python tests/benchmarks/benchmark_cli_session.py --repeats 2 --judge
    python tests/benchmarks/benchmark_cli_session.py --session long --repeats 1
    python tests/benchmarks/benchmark_cli_session.py --report

Live: needs `claude auth login` (or ~/.llm_light_automation_token), refuses to run when an
API key is configured, and spends subscription quota. Not collected by pytest. Run from the
repository root.
"""
import argparse
import contextlib
import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
# chat() adds to the stats file; a benchmark's turns are not the user's.
os.environ.setdefault("LLM_LIGHT_STATS_FILE", str(Path(tempfile.gettempdir()) / "llm_light_bench_stats.json"))

import llm_light.state as state  # noqa: E402
from llm_light import config, indexer, llm, permissions  # noqa: E402
from llm_light.providers import claude  # noqa: E402

DEFAULT_OUT = ROOT / ".llm_light_cache" / "bench" / "cli_session.jsonl"
ARMS = ("resume", "flat")
_ROUTING = [
    ("How does route_query decide the model and effort level for a question?", "llm_light/llm.py"),
    ("What happens to that choice when ROUTE_ONCE is on?", "llm_light/llm.py"),
    ("Where does the budget cap come into it?", "llm_light/llm.py"),
    ("How does the retrieved context reach the claude CLI on the subscription path?", "llm_light/providers/claude.py"),
    ("What does transcript() do with earlier turns?", "llm_light/providers/base.py"),
    ("Which tests cover the route-once behaviour?", "tests/unit/test_llm.py"),
]
_INDEXING = [
    ("How does the indexer split a source file into chunks?", "llm_light/indexer.py"),
    ("Why does a watchdog path go through _project_path?", "llm_light/indexer.py"),
    ("What does the disk cache store, and how is it loaded safely?", "llm_light/indexer.py"),
    ("How does retrieval combine static and dense scores?", "llm_light/retrieval.py"),
    ("What are the exclusions for test files in that?", "llm_light/retrieval.py"),
    ("Give me one concrete risk in that code and how you would test for it.", "llm_light/retrieval.py"),
]
# Past MAX_HISTORY_TURNS, so the history is compressed and the CLI session starts over.
SESSIONS = {"routing": _ROUTING, "indexing": _INDEXING, "long": _ROUTING + _INDEXING}


def _answer_quality():
    path = Path(__file__).resolve().parent / "benchmark_answer_quality.py"
    spec = importlib.util.spec_from_file_location("benchmark_answer_quality", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fresh_conversation() -> None:
    state.conversation_history.clear()
    state.checkpoints.clear()
    state.replies.clear()
    state.turn_count = 0
    state.cli_session = None
    state.cli_tree = None
    state.pinned_model = config.MODEL_SONNET


def run_session(arm: str, session: str, repeat: int) -> list:
    """One session on one arm. Returns a row per turn; a turn that failed has an error."""
    _fresh_conversation()
    permissions.set_mode("plan")
    config.CLI_RESUME = arm == "resume"
    calls = []
    real_describe = claude._describe_tool_call

    def describe(name, tool_input):
        label = real_describe(name, tool_input)
        calls.append(label)
        return label

    claude._describe_tool_call = describe
    rows = []
    try:
        for turn, (question, source) in enumerate(SESSIONS[session]):
            calls.clear()
            before = dict(state.session_tokens)
            cost_before = state.session_cost
            replies_before = len(state.replies)
            start = time.monotonic()
            llm.chat(question)
            tokens = {k: state.session_tokens[k] - before[k]
                      for k in ("input", "cache_read", "cache_write", "output")}
            answered = len(state.replies) > replies_before
            rows.append({
                "session": session, "repeat": repeat, "arm": arm, "turn": turn, "question": question,
                "file": source, **tokens, "cost": round(state.session_cost - cost_before, 6),
                "tools": len(calls), "seconds": round(time.monotonic() - start, 1),
                "reply": state.replies[-1] if answered else "", "error": not answered,
                "resumed": bool(state.cli_session) and turn > 0,
            })
    finally:
        claude._describe_tool_call = real_describe
    return rows


def judge_session(rows: list, judge_model: str) -> list:
    """One verdict per turn answered by both arms of one repeat. x is resume, y is flat."""
    aq = _answer_quality()
    by_turn = {(r["turn"], r["arm"]): r for r in rows}
    out = []
    for (turn, arm), row in sorted(by_turn.items()):
        other = by_turn.get((turn, "flat"))
        if arm != "resume" or other is None or row["error"] or other["error"]:
            continue
        earlier = by_turn.get((turn - 1, "resume"))
        # A follow-up ("what happens to that choice") only makes sense after what it follows.
        question = (f"(After asking: {earlier['question']})\n" if earlier else "") + row["question"]
        outcome, verdicts = aq.judge(question, aq.reference_for({"file": row["file"]}), row["reply"],
                                     other["reply"], model=judge_model)
        out.append({"judged": True, "session": row["session"], "repeat": row["repeat"], "turn": turn,
                    "question": row["question"], "outcome": outcome, "verdicts": verdicts,
                    "resume_tools": row["tools"], "flat_tools": other["tools"]})
    return out


def _score_sums(rows: list) -> dict:
    """Each arm's 0-2 scores added up over both judging orders. A win can rest on a detail
    the judge prefers at equal scores, so a saving is read against these too."""
    resume = flat = scored = 0
    for row in rows:
        for shown_first, verdict in zip(("resume", "flat"), row.get("verdicts") or ()):
            if verdict and isinstance(verdict.get("score_a"), int) and isinstance(verdict.get("score_b"), int):
                a, b = verdict["score_a"], verdict["score_b"]
                resume += a if shown_first == "resume" else b
                flat += b if shown_first == "resume" else a
                scored += 1
    return {"resume_score": resume, "flat_score": flat, "scored_verdicts": scored}


def summarize(rows: list) -> dict:
    """Per arm: means per session of every token kind, cost and tool calls, and the verdicts."""
    out = {}
    for arm in ARMS:
        turns = [r for r in rows if r.get("arm") == arm]
        sessions = {(r["session"], r["repeat"]) for r in turns}
        if not sessions:
            continue
        mean = lambda key: sum(r[key] for r in turns) / len(sessions)  # noqa: E731
        out[arm] = {"sessions": len(sessions), "turns": len(turns), "failed": sum(r["error"] for r in turns),
                    "cost": mean("cost"), "input": mean("input"), "cache_read": mean("cache_read"),
                    "cache_write": mean("cache_write"), "output": mean("output"), "tools": mean("tools"),
                    "seconds": mean("seconds")}
    verdicts = [r["outcome"] for r in rows if r.get("judged")]
    if verdicts:
        out["judged"] = {name: verdicts.count(key) for name, key in
                         (("resume_wins", "x"), ("flat_wins", "y"), ("ties", "tie"), ("invalid", "invalid"))}
        out["judged"].update(_score_sums(rows))
    return out


def report(rows: list) -> None:
    summary = summarize(rows)
    for arm in ARMS:
        s = summary.get(arm)
        if s:
            print(f"{arm}: {s['sessions']} sessions, {s['turns']} turns, {s['failed']} failed.  per session: "
                  f"${s['cost']:.4f}, write {s['cache_write']:,.0f}, read {s['cache_read']:,.0f}, "
                  f"input {s['input']:,.0f}, output {s['output']:,.0f}, {s['tools']:.1f} tool calls, "
                  f"{s['seconds']:.0f} s")
    if "judged" in summary:
        print("judged:", summary["judged"])


def load_rows(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--session", choices=(*SESSIONS, "both"), default="both",
                        help="both: routing and indexing, 6 turns each")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--first-repeat", type=int, default=0, help="continue a run: its order keeps alternating")
    parser.add_argument("--judge", action="store_true")
    parser.add_argument("--judge-model", default=config.MODEL_OPUS)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--report", action="store_true", help="only print the summary of --out")
    args = parser.parse_args(argv)

    if args.report:
        report(load_rows(args.out))
        return 0
    if config.AUTH_MODE != "OAUTH":
        print("benchmark_cli_session runs on the subscription only (OAUTH mode); an API key is "
              "configured, and every call would be billed. Unset ANTHROPIC_API_KEY.", file=sys.stderr)
        return 2

    llm._update_skeleton()
    indexer.index_files(quiet=True, dense="skip")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    sessions = ("routing", "indexing") if args.session == "both" else (args.session,)
    for repeat in range(args.first_repeat, args.first_repeat + args.repeats):
        for session in sessions:
            order = ARMS if repeat % 2 == 0 else tuple(reversed(ARMS))
            rows = []
            for arm in order:
                with contextlib.redirect_stdout(sys.stderr):  # chat() prints the replies
                    rows += run_session(arm, session, repeat)
            if args.judge:
                with contextlib.redirect_stdout(sys.stderr):
                    rows += judge_session(rows, args.judge_model)
            with args.out.open("a", encoding="utf-8") as f:
                f.writelines(json.dumps(r) + "\n" for r in rows)
            print(f"{session} repeat {repeat} done", flush=True)
            report(rows)
    report(load_rows(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
