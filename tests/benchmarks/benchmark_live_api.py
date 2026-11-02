"""Live API-key checks: the items no offline test or subscription run can settle.

A subscription turn goes through the `claude` CLI, which builds its own request, so
only an API key reaches the request llm_light builds. One run of each experiment
answers what its issue still has open:

  sdk          #71  anthropic 2.x: a real create and stream parse on Haiku
  effort       #88  effort-only messages are accepted; cache reads survive an effort change
  toptier      #92  Fable 5.1 and Opus 5.5: requests accepted, cache reads, tools, prices
  fetch-chunk  #85  does fetch_chunk pay for its schema? same questions with and without it

Every turn goes through the real chat() and transport. The router is pinned to the
model and effort the experiment names; nothing else is replaced.

Bills the key. It refuses to start without --i-accept-the-bill, in OAuth mode, or on
a subscription token, and stops at --max-usd. Run it from the repo root:

    ANTHROPIC_API_KEY=sk-ant-api... python tests/benchmarks/benchmark_live_api.py \\
        --i-accept-the-bill --experiment sdk
"""
import argparse
import contextlib
import json
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from llm_light import indexer, llm, providers, state, tools  # noqa: E402
from llm_light import config  # noqa: E402
from llm_light.providers import claude as claude_mod  # noqa: E402
from llm_light.ui import calculate_cost  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "repo_questions.json"
DEFAULT_OUT = ROOT / ".llm_light_cache" / "bench" / "live_api.json"
EXPERIMENTS = ("sdk", "effort", "toptier", "fetch-chunk")
_MAX_TOKENS = 4_000
_SEQUENCE = (  # #88: two Sonnet efforts, a Haiku turn, then Sonnet at medium again
    (config.MODEL_SONNET, "medium", "Which file implements the retry backoff? One sentence."),
    (config.MODEL_SONNET, "high", "Which errors does it retry? One sentence."),
    (config.MODEL_HAIKU, "low", "Name the decorator. One word."),
    (config.MODEL_SONNET, "medium", "Which errors does it not retry? One sentence."),
)
_TOOL_QUESTION = "Open llm_light/retry.py with read_file and name the errors it retries. One sentence."
_FOLLOW_UP = "Which of those is a network error? One sentence."


class SpendCap(Exception):
    pass


class Run:
    """Collects one row per provider call and enforces the spend cap."""

    def __init__(self, max_usd: float):
        self.max_usd = max_usd
        self.spent = 0.0
        self.report = {}
        self.failed = False

    def record(self, experiment: str, row: dict) -> None:
        self.report.setdefault(experiment, []).append(row)
        self.spent += row.get("usd", 0.0)
        if row.get("error"):
            self.failed = True
        if self.spent >= self.max_usd:
            raise SpendCap(f"spend cap reached: ${self.spent:.4f} of ${self.max_usd:.2f}")


def _usage_row(usage, model: str) -> dict:
    return {
        "input": getattr(usage, "input_tokens", 0),
        "cache_write": getattr(usage, "cache_creation_input_tokens", 0),
        "cache_read": getattr(usage, "cache_read_input_tokens", 0),
        "output": getattr(usage, "output_tokens", 0),
        "usd": calculate_cost(usage, model),
    }


def _effort_in_prefix(create_kwargs: dict, effort: str) -> None:
    """The request shape before #88: the turn's effort at the top level."""
    create_kwargs["output_config"] = {"effort": effort}


def _reset_conversation() -> None:
    state.conversation_history.clear()
    state.checkpoints.clear()
    state.turn_count = 0


def _cold_skeleton() -> None:
    """The repo's skeleton with a unique line, so this run reads no earlier run's cache."""
    llm._update_skeleton()
    state.skeleton_context += f"\n<!-- live check {uuid.uuid4()} -->\n"


def _turn(run: Run, experiment: str, label: str, model: str, effort: str, question: str,
          use_index: bool = False, **extra) -> dict | None:
    """One real chat() turn on model at effort. Returns its row, or None if the turn
    made no provider call. chat() prints a failure and returns, so the provider call
    is what records the error."""
    provider = providers.active()
    real_complete = provider.complete
    real_run_tool = tools.run_tool
    called = []
    tool_calls = []

    def complete(request):
        row = dict(label=label, model=model, effort=effort, tool_calls=tool_calls, **extra)
        called.append(row)
        try:
            result = real_complete(request)
        except Exception as e:
            row["error"] = str(e)
            raise
        row.update(_usage_row(result[1], model))
        return result

    def run_tool(name, tool_input):
        tool_calls.append(name)
        return real_run_tool(name, tool_input)

    state.pinned_model = model
    with patch.object(provider, "complete", complete), \
         patch.object(tools, "run_tool", run_tool), \
         patch.object(llm, "route_query", return_value=(model, effort, _MAX_TOKENS)):
        llm.chat(question, use_index=use_index)
    for row in called:
        run.record(experiment, row)
    if any(row.get("error") for row in called):
        raise RuntimeError(called[-1]["error"])
    return called[-1] if called else None


def exp_sdk(run: Run, args) -> None:
    """#71: one create and one stream through the client llm_light uses."""
    _cold_skeleton()
    system = llm._build_system_blocks(state.skeleton_context)
    message = [{"role": "user", "content": [{"type": "text", "text": "Reply with the word PONG."}]}]
    kwargs = dict(model=config.MODEL_HAIKU, max_tokens=512, system=system, messages=message,
                  extra_headers=llm._EXTENDED_CACHE_HEADERS, output_config={"effort": llm.BASE_EFFORT})
    response = claude_mod.client.messages.create(**kwargs)
    run.record("sdk", dict(call="messages.create", model=config.MODEL_HAIKU,
                           stop_reason=response.stop_reason,
                           request_id=getattr(response, "_request_id", None),
                           **_usage_row(response.usage, config.MODEL_HAIKU)))
    stream_kwargs = dict(kwargs, messages=list(message))
    llm._apply_effort(stream_kwargs, "low")
    _, usage, _ = providers.active().complete(stream_kwargs)
    run.record("sdk", dict(call="messages.stream", model=config.MODEL_HAIKU,
                           **_usage_row(usage, config.MODEL_HAIKU)))


def exp_effort(run: Run, args) -> None:
    """#88: the session the issue names. --effort-in-prefix runs the old shape to compare."""
    _cold_skeleton()
    _reset_conversation()
    apply = _effort_in_prefix if args.effort_in_prefix else llm._apply_effort
    with patch.object(llm, "_apply_effort", apply):
        for i, (model, effort, question) in enumerate(_SEQUENCE, 1):
            _turn(run, "effort", f"turn {i}", model, effort, question,
                  effort_in_prefix=args.effort_in_prefix)


def exp_toptier(run: Run, args) -> None:
    """#92: two turns per top-tier model; the second must read what the first wrote."""
    for model in (config.MODEL_FABLE, config.MODEL_OPUS):
        _cold_skeleton()
        _reset_conversation()
        _turn(run, "toptier", "tool turn", model, "medium", _TOOL_QUESTION)
        _turn(run, "toptier", "follow-up", model, "medium", _FOLLOW_UP)


def _build_index() -> bool:
    """True once retrieval and fetch_chunk can use the index."""
    indexer.index_files(dense="skip")
    return True


def exp_fetch_chunk(run: Run, args) -> None:
    """#85: the same questions with and without fetch_chunk in TOOLS.

    Removing a tool changes the cached prefix, so each arm writes its own. Compare
    warm turns: `summary` leaves each arm's first question out.
    """
    questions = json.loads(FIXTURE.read_text(encoding="utf-8"))["questions"][:args.limit]
    use_index = _build_index()
    with_tool = list(tools.TOOLS)
    without_tool = [t for t in with_tool if t["name"] != "fetch_chunk"]
    for arm, arm_tools in (("with", with_tool), ("without", without_tool)):
        _cold_skeleton()
        with patch.object(tools, "TOOLS", arm_tools):
            for q in questions:
                _reset_conversation()
                _turn(run, "fetch-chunk", q["question"], config.MODEL_SONNET, "medium",
                      q["question"], use_index=use_index, arm=arm)


_RUNNERS = {"sdk": exp_sdk, "effort": exp_effort, "toptier": exp_toptier,
            "fetch-chunk": exp_fetch_chunk}


def _print_table(report: dict, stream) -> None:
    for experiment, rows in report.items():
        print(f"\n{experiment}", file=stream)
        print("| Turn | Model | Effort | Input | Cache write | Cache read | Output | USD | Tools | Error |",
              file=stream)
        print("|---|---|---|---|---|---|---|---|---|---|", file=stream)
        for r in rows:
            name = r.get("call") or r.get("label", "")
            if r.get("arm"):
                name = f"{r['arm']}: {name[:40]}"
            print(f"| {name} | {r.get('model', '')} | {r.get('effort', '')} | {r.get('input', 0):,} "
                  f"| {r.get('cache_write', 0):,} | {r.get('cache_read', 0):,} | {r.get('output', 0):,} "
                  f"| {r.get('usd', 0.0):.4f} | {len(r.get('tool_calls', []))} | {r.get('error', '')} |",
                  file=stream)


def _arm_summary(rows: list) -> dict:
    """Mean tokens per warm question for each fetch_chunk arm (each arm's first is cold)."""
    out = {}
    for arm in ("with", "without"):
        warm = [r for r in rows if r.get("arm") == arm][1:]
        if warm:
            out[arm] = {k: round(sum(r.get(k, 0) for r in warm) / len(warm), 1)
                        for k in ("input", "cache_write", "cache_read", "output")}
            out[arm]["tool_calls"] = round(sum(len(r["tool_calls"]) for r in warm) / len(warm), 2)
            out[arm]["questions"] = len(warm)
    return out


def _refusal(args) -> str | None:
    if not args.i_accept_the_bill:
        return "This bills your API key. Pass --i-accept-the-bill to run it."
    if config.AUTH_MODE != "API_KEY":
        return "No API key is configured (OAUTH mode). Set ANTHROPIC_API_KEY."
    if str(config.API_KEY or "").startswith("sk-ant-oat"):
        return ("That is a subscription token, not an API key. Subscription runs go through "
                "the claude CLI; use benchmark_answer_quality.py for those.")
    return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--experiment", choices=EXPERIMENTS + ("all",), default="sdk")
    parser.add_argument("--i-accept-the-bill", action="store_true",
                        help="Required: every experiment sends billed requests.")
    parser.add_argument("--effort-in-prefix", action="store_true",
                        help="effort only: send each turn's effort at the top level, the shape before #88.")
    parser.add_argument("--limit", type=int, default=10, help="fetch-chunk: questions per arm")
    parser.add_argument("--max-usd", type=float, default=1.00, help="stop once this much is spent")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    refusal = _refusal(args)
    if refusal:
        print(refusal, file=sys.stderr)
        return 2

    run = Run(args.max_usd)
    saved_pin = state.pinned_model
    code = 0
    try:
        with contextlib.redirect_stdout(sys.stderr):  # chat() prints the replies
            for name in (EXPERIMENTS if args.experiment == "all" else (args.experiment,)):
                _RUNNERS[name](run, args)
    except SpendCap as e:
        print(str(e), file=sys.stderr)
        code = 1
    except Exception as e:
        print(f"stopped: {e}", file=sys.stderr)
        code = 1
    finally:
        state.pinned_model = saved_pin

    if run.failed:
        code = 1
    report = dict(run.report)
    if "fetch-chunk" in report:
        report["fetch-chunk-summary"] = _arm_summary(report["fetch-chunk"])
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    _print_table(run.report, sys.stdout)
    print(f"\nspent about ${run.spent:.4f} at MODEL_PRICES; rows in {args.out}")
    return code


if __name__ == "__main__":
    sys.exit(main())
