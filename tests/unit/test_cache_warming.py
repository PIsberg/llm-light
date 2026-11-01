"""What the prompt cache is kept warm with, and how often.

A warm request is not free: it bills a cache read of the whole prefix (tools,
system prompt, skeleton), or a 1-hour cache write when the entry is missing.
These tests pin the three rules that keep warms from costing more than they
save:

* The heartbeat refreshes the skeleton entry once per TTL, not every tick.
  A cache read resets the entry's timer, so one read just inside the 1-hour
  TTL keeps it alive; reading it every 30 seconds buys nothing.
* A warm targets the model the turns will read. The cache is per model, so
  warming Sonnet while `--model opus` turns read Opus is a pure extra cost.
* No warm before the model is known. Most questions route to Haiku, and the
  first turn writes its own model's entry anyway.

A lint retry also keeps its cache breakpoints, so the retry reads the history
and the retrieved chunks the failed attempt just wrote.
"""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy-test-key")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import llm_light
import llm_light.state as state
from llm_light import config, llm, main

ONE_HOUR = 3600


def _usage():
    usage = MagicMock()
    usage.input_tokens = 10
    usage.output_tokens = 0
    usage.cache_creation_input_tokens = 0
    usage.cache_read_input_tokens = 0
    return usage


class _FakeClock:
    """Stands in for time.time() and state.stop_event: each wait() advances
    the clock by its timeout, and the event reads as set once `end` passes."""

    def __init__(self, start, end):
        self.now = start
        self.end = end

    def time(self):
        return self.now

    def wait(self, timeout=None):
        self.now += timeout
        return self.is_set()

    def is_set(self):
        return self.now >= self.end


def _run_heartbeat(idle_seconds, last_turn_ago=0.0):
    """Run the real heartbeat over `idle_seconds` of fake time after a turn
    `last_turn_ago` seconds before the start; return the times it warmed."""
    start = 1_000_000.0
    clock = _FakeClock(start, start + idle_seconds)
    warms = []

    def fake_warm(quiet=False):
        warms.append(clock.now)

    # Once any wrapped call has run, main.heartbeat is llm_light's wrapper,
    # whose _sync_bindings() copies the package-level stop_event,
    # last_interaction and warm_cache into state, main and llm. Patch both
    # layers, or the heartbeat waits on the real stop event forever.
    saved = (state.stop_event, state.last_interaction)
    try:
        state.stop_event = clock
        state.last_interaction = start - last_turn_ago
        with patch("time.time", clock.time), \
             patch.object(llm_light, "stop_event", clock), \
             patch.object(llm_light, "last_interaction", start - last_turn_ago), \
             patch.object(llm_light, "warm_cache", fake_warm), \
             patch.object(main, "warm_cache", fake_warm), \
             patch.object(llm, "warm_cache", llm.warm_cache):
            main.heartbeat()
    finally:
        state.stop_event, state.last_interaction = saved
    return [w - start for w in warms], -last_turn_ago


class TestHeartbeatPacing(unittest.TestCase):

    def test_an_idle_hour_costs_at_most_one_warm(self):
        warms, _ = _run_heartbeat(ONE_HOUR)
        self.assertLessEqual(len(warms), 1, f"warmed {len(warms)} times in one idle hour")

    def test_the_entry_is_refreshed_before_its_ttl_runs_out(self):
        # Over three idle hours, no gap between cache uses may reach the TTL,
        # or the next turn pays a fresh 1-hour write.
        warms, last_turn = _run_heartbeat(3 * ONE_HOUR)
        uses = [last_turn] + warms
        gaps = [b - a for a, b in zip(uses, uses[1:] + [3 * ONE_HOUR])]
        self.assertTrue(warms, "never warmed in three idle hours")
        self.assertLess(max(gaps), ONE_HOUR, f"cache uses at {uses}")

    def test_three_idle_hours_need_three_warms_at_most(self):
        warms, _ = _run_heartbeat(3 * ONE_HOUR)
        self.assertLessEqual(len(warms), 3)

    def test_a_recent_turn_postpones_the_warm(self):
        # The turn itself read the entry, which reset its timer.
        warms, _ = _run_heartbeat(10 * 60, last_turn_ago=0)
        self.assertEqual(warms, [])


class TestWarmModel(unittest.TestCase):

    def setUp(self):
        self._saved = (state.session_model, state.pinned_model)
        state.session_model = None
        state.pinned_model = None

    def tearDown(self):
        state.session_model, state.pinned_model = self._saved

    def _warmed_models(self):
        resp = MagicMock()
        resp.usage = _usage()
        # llm_light.warm_cache wraps the real function; llm.warm_cache may still
        # be a mock another test's patch left behind through _sync_bindings().
        with patch("llm_light.providers.claude.client.messages.create", return_value=resp) as create:
            llm_light.warm_cache(quiet=True)
        return [c.kwargs["model"] for c in create.call_args_list]

    def test_no_warm_before_the_model_is_known(self):
        # A Sonnet warm at startup is wasted whenever the first question
        # routes to Haiku, which most repo questions do.
        self.assertEqual(self._warmed_models(), [])

    def test_warms_the_pinned_model(self):
        state.pinned_model = config.MODEL_OPUS
        self.assertEqual(self._warmed_models(), [config.MODEL_OPUS])

    def test_the_pin_wins_over_the_session_model(self):
        # /model after a routed turn: the next turns run on the pinned model.
        state.session_model = config.MODEL_HAIKU
        state.pinned_model = config.MODEL_OPUS
        self.assertEqual(self._warmed_models(), [config.MODEL_OPUS])

    def test_warms_the_session_model(self):
        state.session_model = config.MODEL_HAIKU
        self.assertEqual(self._warmed_models(), [config.MODEL_HAIKU])


def _breakpoints(request):
    found = []
    for block in request["system"]:
        if "cache_control" in block:
            found.append(("system", block["text"][:20]))
    for i, m in enumerate(request["messages"]):
        if isinstance(m["content"], list):
            for b in m["content"]:
                if isinstance(b, dict) and "cache_control" in b:
                    found.append((i, b.get("text", "")[:30]))
    return found


class TestLintRetryKeepsTheCache(unittest.TestCase):

    def setUp(self):
        self._saved = (list(state.conversation_history), state.turn_count, state.session_model)
        state.conversation_history[:] = [
            {"role": "user", "content": "earlier question"},
            {"role": "assistant", "content": "earlier answer"},
        ]

    def tearDown(self):
        hist, state.turn_count, state.session_model = self._saved
        state.conversation_history[:] = hist

    def _retry_requests(self):
        sent = []

        def fake_call(**kwargs):
            sent.append(json.loads(json.dumps(kwargs, default=str)))
            return f"reply {len(sent)}", _usage(), False

        edit = {"path": "a.py", "type": "edit", "search": "x", "replace": "y("}
        with patch("llm_light.llm.route_query", return_value=(config.MODEL_SONNET, "high", 8_192)), \
             patch("llm_light.providers.claude._make_streaming_api_call", side_effect=fake_call), \
             patch("llm_light.llm.retrieve", return_value=("def a(): pass", [])), \
             patch("llm_light.llm.parse_edit_blocks", side_effect=[[edit], []]), \
             patch("llm_light.llm.apply_edits", return_value=["a.py: syntax error"]), \
             patch("llm_light.llm._maybe_compress_history"), \
             patch("llm_light.llm._print_reply"), \
             patch("llm_light.llm.print_stats"), \
             patch("llm_light.config.LLMLINGUA_ENABLED", False):
            llm.chat("fix a")
        return sent

    def test_the_retry_reads_the_history_and_chunks_the_first_attempt_wrote(self):
        first, retry = self._retry_requests()
        self.assertEqual(_breakpoints(retry), _breakpoints(first))
        texts = [t for _, t in _breakpoints(retry)]
        self.assertIn("earlier answer", texts)
        self.assertTrue(any(t.startswith("Retrieved Codebase Context") for t in texts))

    def test_the_retry_stays_within_four_breakpoints(self):
        # The API rejects a fifth; the tool loop may add one on a tool result.
        _, retry = self._retry_requests()
        self.assertLessEqual(len(_breakpoints(retry)), 3)


if __name__ == "__main__":
    unittest.main()
