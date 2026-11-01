#!/usr/bin/env python3
"""
Cache warming cost: what the startup warm and the idle heartbeat spend.

Runs the real warm_cache() and heartbeat() against a fake API client and a
fake clock. The fake client keeps a prompt cache per model for the prefix
(tools, system prompt and skeleton, measured from this repo at 4 characters
per token, 1-hour TTL, refreshed by every read) and prices each request with
MODEL_PRICES: a read when the model's entry is alive, a 1-hour write when it
is not. Turns are modelled as one prefix read or write on their model, so a
warm that a turn never reads shows up as extra cost, and an entry the heartbeat
let expire shows up as a turn that writes again.

Scenario per row: start the session (startup warm), ask one question on the
opening model at minute 1, stay idle for --idle hours with the heartbeat
running, then ask a second question on the same model. Rows:

  routed, opens on <model>   ROUTE_ONCE session, first question routes there
  pinned <model>             --model pins it; no routing

Only the prefix is priced: history, retrieved chunks and output are the same
under every warming policy, so they cancel out of the comparison. This is a
cost model of the warming policy, not a live measurement.

    python tests/benchmarks/benchmark_cache_warming.py
    python tests/benchmarks/benchmark_cache_warming.py --idle 8

Not collected by pytest. Run from the repo root. No API key, no network.
"""
import argparse
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
# The mock key selects API-key mode with a client that sends nothing.
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-mock-key"

import llm_light.state as state  # noqa: E402
from llm_light import config, llm, main  # noqa: E402
from llm_light.providers import claude  # noqa: E402

TTL = 3600
START = 1_000_000.0


def prefix_tokens():
    """Skeleton + tools + system prompt, at 4 characters per token."""
    from llm_light import tools
    from llm_light.skeleton import build_skeleton
    chars = len(build_skeleton()) + len(json.dumps(tools.TOOLS)) + len(config.SYSTEM_PROMPT)
    return chars // 4


class Clock:
    """time.time() and state.stop_event in one: wait() advances the clock."""

    def __init__(self):
        self.now = START
        self.end = START

    def time(self):
        return self.now

    def wait(self, timeout=None):
        self.now += timeout
        return self.is_set()

    def is_set(self):
        return self.now >= self.end


class FakeCache:
    """A per-model 1-hour prompt cache for the prefix, priced per request."""

    def __init__(self, clock, prefix):
        self.clock, self.prefix = clock, prefix
        self.used = {}            # model -> time of the last read or write
        self.cost = {"warm": 0.0, "turn": 0.0}
        self.calls = {"warm": 0, "turn": 0}

    def use(self, model, kind):
        p = config.MODEL_PRICES[model]
        alive = model in self.used and self.clock.now - self.used[model] < TTL
        rate = p["read"] if alive else p["write_1h"]
        self.cost[kind] += self.prefix * rate / 1e6
        self.calls[kind] += 1
        self.used[model] = self.clock.now

    def create(self, **kwargs):           # client.messages.create, for warms
        self.use(kwargs["model"], "warm")
        return SimpleNamespace(usage=SimpleNamespace(
            input_tokens=0, output_tokens=0, cache_creation_input_tokens=0,
            cache_read_input_tokens=0, cache_creation={}))


def run(opener, pinned, idle_hours, prefix):
    clock = Clock()
    cache = FakeCache(clock, prefix)
    client = SimpleNamespace(messages=SimpleNamespace(create=cache.create))
    saved = (state.stop_event, state.last_interaction, state.session_model, state.pinned_model)
    try:
        state.stop_event = clock
        state.session_model, state.pinned_model = None, pinned
        with patch("time.time", clock.time), \
             patch.object(claude, "client", client), \
             patch.object(claude, "_accumulate_usage", lambda *a, **k: None), \
             patch.object(claude, "calculate_cost", lambda *a, **k: 0.0), \
             patch("llm_light.config.ROUTE_ONCE", True):
            state.last_interaction = clock.now
            llm.warm_cache(quiet=True)                    # startup, as full_refresh()
            for turn in range(2):
                clock.now += 60
                model = pinned or opener
                if not pinned:
                    state.session_model = model           # what _finalize_route() pins
                cache.use(model, "turn")
                state.last_interaction = clock.now
                if turn == 0:
                    clock.end = clock.now + idle_hours * 3600
                    main.heartbeat()
    finally:
        state.stop_event, state.last_interaction, state.session_model, state.pinned_model = saved
    return cache


def main_():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--idle", type=float, default=1.0, help="idle hours between the two questions (default 1)")
    args = ap.parse_args()
    prefix = prefix_tokens()
    print(f"prefix {prefix:,} tokens; idle {args.idle:g} h between two questions; "
          f"heartbeat every {config.HEARTBEAT_SECS} s, warm after {config.CACHE_TTL_SECS} s")
    print(f"  {'session':28} {'warms':>6} {'warm $':>9} {'turn $':>9} {'total $':>9}")
    rows = [(f"routed, opens on {m.split('-')[1]}", m, None)
            for m in (config.MODEL_HAIKU, config.MODEL_SONNET, config.MODEL_OPUS)]
    rows += [(f"pinned {m.split('-')[1]}", None, m) for m in (config.MODEL_HAIKU, config.MODEL_OPUS)]
    for label, opener, pinned in rows:
        c = run(opener, pinned, args.idle, prefix)
        total = c.cost["warm"] + c.cost["turn"]
        print(f"  {label:28} {c.calls['warm']:>6} {c.cost['warm']:>9.4f} {c.cost['turn']:>9.4f} {total:>9.4f}")


if __name__ == "__main__":
    main_()
