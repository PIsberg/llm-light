#!/usr/bin/env python3
"""
Answer quality, live: two variants answer the same repo question, a judge model
picks the better answer. Runs on the Claude subscription (OAUTH mode) through
llm_light's own CLI transport, so it bills no API key.

Experiments (each pairs a variant x with a variant y on the same question):

  models     Sonnet-routed questions answered by Haiku (x) and Sonnet (y), same
             retrieved context. Does ROUTE_ONCE cost quality when a session that
             opened on Haiku keeps Haiku for Sonnet questions? (issue #90)
  budget     High-routed questions with the medium retrieval budget (x) and the
             high one (y), both rendered at high effort. Can the high budget drop
             to 3,000 tokens? (issue #77)
  toptier    No judge: one question each on Fable 5.1 and Opus 5.5 through the
             real agent path in plan mode (tools on, read-only), twice, to see the
             model answer, use its tools, and read the cache on the second call.
             (issue #92)

How a pair is built and scored:

  * Retrieval is llm_light's own: hybrid static + dense scores, test chunks
    excluded, _hybrid_select_pairs, then retrieval._finish to render. The
    working-tree boost is left out so a dirty checkout cannot move the results.
    (A callgraph experiment answered #86 on 2026-11-02 and went with the
    expansion it measured; its results are in docs/BENCHMARKS.md section 2f.)
  * The request is built as chat() builds it (the skeleton system blocks, the
    retrieved context, the question block), flattened by transcript(), and sent
    with providers.claude._make_cli_subprocess_call. Tools are off, so the answer
    rests on the context alone: the variable under test is not diluted by the
    agent reading files. The CLI path carries no effort, so every answer runs at
    the CLI's default effort.
  * The judge (Opus 5.5 by default, tools off) sees the question, a reference
    (the answering file's source), and the two
    answers, then scores each 0-2 and names a winner. Each pair is judged twice
    with the answers swapped; a side wins only when both orders agree.

Rows are appended to --out as JSON lines, and a re-run skips the pairs already
there, so an interrupted run resumes.

    python tests/benchmarks/benchmark_answer_quality.py --experiment all
    python tests/benchmarks/benchmark_answer_quality.py --experiment models --limit 2
    python tests/benchmarks/benchmark_answer_quality.py --report

Live: needs `claude auth login` (or ~/.llm_light_automation_token) and refuses
to run when an API key is configured. Not collected by pytest. Run from the
repository root.
"""
import argparse
import contextlib
import importlib.util
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import llm_light.state as state  # noqa: E402
from llm_light import config, llm, permissions, retrieval  # noqa: E402
from llm_light.providers import claude  # noqa: E402
from llm_light.providers.base import transcript  # noqa: E402

REPO_FIXTURE = ROOT / "tests" / "fixtures" / "repo_questions.json"
DEFAULT_OUT = ROOT / ".llm_light_cache" / "bench" / "answer_quality.jsonl"
EXPERIMENTS = ("models", "budget")
JUDGE_MODEL = config.MODEL_OPUS
REFERENCE_CHARS = 16_000
TOPTIER_QUESTION = "Which function decides the model and effort level a question gets, and how?"

JUDGE_SYSTEM = (
    "You grade answers to questions about a software repository. You are given the "
    "question, reference material from the repository, and two answers, A and B. Judge "
    "only correctness against the reference and how completely each answer addresses "
    "the question; ignore length and style. Score each answer 0 (wrong or unsupported), "
    "1 (partly right or incomplete) or 2 (right and complete). Then name the better "
    "answer, or tie when neither is better. End your reply with one line of JSON: "
    '{"winner": "A" | "B" | "tie", "score_a": 0-2, "score_b": 0-2}'
)


# --- judging ----------------------------------------------------------------

_OBJECT = re.compile(r"\{[^{}]*\}")


def parse_verdict(text: str):
    """The last JSON object in the judge's reply with a valid winner, or None."""
    for raw in reversed(_OBJECT.findall(text or "")):
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if obj.get("winner") in ("A", "B", "tie"):
            return {"winner": obj["winner"], "score_a": obj.get("score_a"),
                    "score_b": obj.get("score_b")}
    return None


def combine(first, second) -> str:
    """'x', 'y', 'tie' or 'invalid' from two judgements: x shown as A, then as B."""
    if first is None or second is None:
        return "invalid"
    if first["winner"] == "A" and second["winner"] == "B":
        return "x"
    if first["winner"] == "B" and second["winner"] == "A":
        return "y"
    return "tie"


def _judge_prompt(question, reference, a, b) -> str:
    return (f"QUESTION:\n{question}\n\nREFERENCE:\n{reference}\n\n"
            f"ANSWER A:\n{a}\n\nANSWER B:\n{b}\n")


def _cli(prompt, model, system, tools=False):
    return claude._make_cli_subprocess_call(
        prompt, model=model, extra_flags=["--system-prompt", system], render=False, tools=tools)


def judge(question, reference, answer_x, answer_y, model=JUDGE_MODEL):
    """(outcome, [verdict x-first, verdict y-first]); see combine()."""
    verdicts = []
    for a, b in ((answer_x, answer_y), (answer_y, answer_x)):
        text, _usage, _sid = _cli(_judge_prompt(question, reference, a, b), model, JUDGE_SYSTEM)
        verdicts.append(parse_verdict(text))
    return combine(*verdicts), verdicts


def reference_for(q) -> str:
    source = (ROOT / q["file"]).read_text(encoding="utf-8", errors="replace")
    if len(source) > REFERENCE_CHARS:
        source = source[:REFERENCE_CHARS] + "\n... (truncated)"
    return f"Source of {q['file']}:\n{source}"


# --- pairs ------------------------------------------------------------------

def _arm(model, effort, budget):
    return {"model": model, "effort": effort, "budget": budget}


def pairs(experiment, questions, route):
    """The (question, x, y) pairs for one experiment. route(q) -> (model, effort, max_tokens)."""
    budgets = config._RETRIEVAL_BUDGET
    out = []
    for q in questions:
        if q["kind"] == "unrelated":
            continue
        model, effort, _ = route(q["question"])
        if experiment == "models" and model == config.MODEL_SONNET:
            out.append({"experiment": experiment, "question": q,
                        "x": _arm(config.MODEL_HAIKU, effort, budgets[effort]),
                        "y": _arm(config.MODEL_SONNET, effort, budgets[effort])})
        elif experiment == "budget" and effort == "high":
            out.append({"experiment": experiment, "question": q,
                        "x": _arm(model, effort, budgets["medium"]),
                        "y": _arm(model, effort, budgets["high"])})
    return out


# --- retrieval and answers --------------------------------------------------

def _benchmark_models():
    path = Path(__file__).resolve().parent / "benchmark_models.py"
    spec = importlib.util.spec_from_file_location("benchmark_models", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Retriever:
    """llm_light's hybrid retrieval over this repo, with cached embeddings."""

    def __init__(self, questions, cache_dir):
        bm = _benchmark_models()
        chunks = bm.collect_chunks()
        self.ids = [c["id"] for c in chunks]
        state.chunk_store.clear()
        state.chunk_store.update({c["id"]: {"text": c["text"]} for c in chunks})
        avg = max(1, sum(len(c["text"]) for c in chunks) // len(chunks) // 4)
        state.TOP_K = max(2, min(15, round(config.TARGET_RETRIEVED_TOKENS / avg)))
        self.index = {q["question"]: i for i, q in enumerate(questions)}
        self.fast = bm.embed(config.FAST_EMBED_MODEL, chunks, questions, cache_dir)
        self.dense = bm.embed(config.DENSE_EMBED_MODEL, chunks, questions, cache_dir)

    def context(self, question, arm) -> tuple:
        i = self.index[question]
        (Ef, Qf), (Ed, Qd) = self.fast, self.dense
        fused = retrieval.hybrid_scores(Ef @ Qf[i], Ed @ Qd[i])
        if fused is None:
            return "", []
        fused = retrieval._exclude_test_chunks(fused, self.ids, question)
        budget = arm["budget"]
        k = max(2, round(state.TOP_K * budget / config.TARGET_RETRIEVED_TOKENS))
        top = retrieval._hybrid_select_pairs(self.ids, fused, budget, k)
        return retrieval._finish(top, arm["effort"])


def _request(skeleton, retrieved_ctx, question, model, effort) -> dict:
    """The create_kwargs chat() builds for a first turn."""
    blocks = []
    if retrieved_ctx:
        blocks.append({"type": "text", "text": f"Retrieved Codebase Context:\n{retrieved_ctx}\n\n",
                       "cache_control": {"type": "ephemeral"}})
    blocks.append({"type": "text", "text": llm._question_text(question, "")})
    request = dict(model=model, max_tokens=4096, system=llm._build_system_blocks(skeleton),
                   messages=[{"role": "user", "content": blocks}])
    llm._apply_effort(request, effort)
    return request


def _usage_row(usage) -> dict:
    get = (lambda k: getattr(usage, k, 0) or 0) if usage is not None else (lambda k: 0)
    return {"input_tokens": get("input_tokens"), "output_tokens": get("output_tokens"),
            "cache_read_input_tokens": get("cache_read_input_tokens"),
            "cache_creation_input_tokens": get("cache_creation_input_tokens")}


def answer(skeleton, retrieved_ctx, question, model, effort) -> dict:
    system_text, prompt = transcript(_request(skeleton, retrieved_ctx, question, model, effort))
    start = time.monotonic()
    text, usage, _sid = _cli(prompt, model, system_text)
    return {"text": text, "seconds": round(time.monotonic() - start, 1), **_usage_row(usage)}


# --- run and report ---------------------------------------------------------

def _key(row):
    return row["experiment"], row["question"]


def load_rows(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def run_pair(pair, skeleton, retriever, judge_model) -> dict:
    q = pair["question"]
    sides = {}
    for side in ("x", "y"):
        arm = pair[side]
        ctx, hits = retriever.context(q["question"], arm)
        sides[side] = {**arm, "chunks": [c for c, _ in hits],
                       "context_tokens": retrieval._estimate_tokens(ctx),
                       **answer(skeleton, ctx, q["question"], arm["model"], arm["effort"])}
    outcome, verdicts = judge(q["question"], reference_for(q), sides["x"]["text"],
                              sides["y"]["text"], model=judge_model)
    return {"experiment": pair["experiment"], "question": q["question"], "file": q.get("file"),
            "outcome": outcome, "verdicts": verdicts, "x": sides["x"], "y": sides["y"]}


def summarize(rows) -> dict:
    out = {}
    for row in rows:
        s = out.setdefault(row["experiment"], {"pairs": 0, "x_wins": 0, "y_wins": 0, "ties": 0,
                                               "invalid": 0, "_x": [], "_y": [], "_xi": [], "_yi": []})
        s["pairs"] += 1
        s[{"x": "x_wins", "y": "y_wins", "tie": "ties"}.get(row["outcome"], "invalid")] += 1
        s["_x"].append(row["x"]["output_tokens"])
        s["_y"].append(row["y"]["output_tokens"])
        s["_xi"].append(row["x"].get("context_tokens", 0))
        s["_yi"].append(row["y"].get("context_tokens", 0))
    for s in out.values():
        for side in ("x", "y"):
            outs, ctx = s.pop(f"_{side}"), s.pop(f"_{side}i")
            s[f"{side}_output_tokens"] = round(sum(outs) / len(outs))
            s[f"{side}_context_tokens"] = round(sum(ctx) / len(ctx))
    return out


_LABELS = {"models": ("Haiku", "Sonnet"), "budget": ("medium budget", "high budget")}


def report(rows) -> None:
    for experiment, s in summarize(rows).items():
        x, y = _LABELS.get(experiment, ("x", "y"))
        print(f"{experiment}: {s['pairs']} pairs  {x} wins {s['x_wins']}  {y} wins {s['y_wins']}"
              f"  ties {s['ties']}  invalid {s['invalid']}")
        print(f"  mean context tokens  {x} {s['x_context_tokens']:,}  {y} {s['y_context_tokens']:,}")
        print(f"  mean output tokens   {x} {s['x_output_tokens']:,}  {y} {s['y_output_tokens']:,}")


def toptier(skeleton) -> list:
    """Two plan-mode turns per top-tier model through the real agent path."""
    rows = []
    previous = permissions.mode()
    permissions.set_mode("plan")
    try:
        for model in (config.MODEL_FABLE, config.MODEL_OPUS):
            for attempt in (1, 2):
                request = _request(skeleton, "", TOPTIER_QUESTION, model, "medium")
                start = time.monotonic()
                try:
                    with contextlib.redirect_stdout(sys.stderr):
                        text, usage, _streamed = claude._make_streaming_api_call(**request)
                    rows.append({"model": model, "call": attempt, "ok": bool(text.strip()),
                                 "seconds": round(time.monotonic() - start, 1),
                                 "reply_chars": len(text), **_usage_row(usage)})
                except Exception as exc:  # the row records the failure
                    rows.append({"model": model, "call": attempt, "ok": False,
                                 "error": f"{type(exc).__name__}: {exc}"})
                print(json.dumps(rows[-1]), flush=True)
    finally:
        permissions.set_mode(previous)
    return rows


def _route_with_static_embedder():
    """route_query with the static embedder, as benchmark_router.py runs it."""
    from sentence_transformers import SentenceTransformer
    state.EMBED_MODEL = config.FAST_EMBED_MODEL
    state.embedder = SentenceTransformer(config.FAST_EMBED_MODEL, device="cpu")
    state.embedder_ready.set()
    return llm.route_query


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--experiment", choices=EXPERIMENTS + ("toptier", "all"), default="all")
    parser.add_argument("--limit", type=int, default=0, help="at most this many pairs per experiment")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--cache-dir", type=Path, default=ROOT / ".llm_light_cache" / "bench")
    parser.add_argument("--judge-model", default=JUDGE_MODEL)
    parser.add_argument("--report", action="store_true", help="only print the summary of --out")
    args = parser.parse_args(argv)

    if args.report:
        report(load_rows(args.out))
        return 0
    if config.AUTH_MODE != "OAUTH":
        print("benchmark_answer_quality runs on the subscription only (OAUTH mode); an API key "
              "is configured, and every call would be billed. Unset ANTHROPIC_API_KEY.",
              file=sys.stderr)
        return 2

    from llm_light import skeleton as skeleton_mod
    skeleton = skeleton_mod.build_skeleton()
    print(f"skeleton {len(skeleton):,} characters", flush=True)

    if args.experiment in ("toptier", "all"):
        print("toptier:", flush=True)
        toptier(skeleton)
        if args.experiment == "toptier":
            return 0

    repo = json.loads(REPO_FIXTURE.read_text(encoding="utf-8"))["questions"]
    route = _route_with_static_embedder()
    retriever = Retriever(repo, args.cache_dir)
    wanted = EXPERIMENTS if args.experiment == "all" else (args.experiment,)
    done = {_key(r) for r in load_rows(args.out)}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    for experiment in wanted:
        todo = pairs(experiment, repo, route)
        if args.limit:
            todo = todo[:args.limit]
        print(f"{experiment}: {len(todo)} pairs", flush=True)
        for pair in todo:
            if (experiment, pair["question"]["question"]) in done:
                continue
            with contextlib.redirect_stdout(sys.stderr):
                row = run_pair(pair, skeleton, retriever, args.judge_model)
            with args.out.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
            print(f"  {row['outcome']:7} {row['question']}", flush=True)
    report([r for r in load_rows(args.out) if r["experiment"] in wanted])
    return 0


if __name__ == "__main__":
    sys.exit(main())
