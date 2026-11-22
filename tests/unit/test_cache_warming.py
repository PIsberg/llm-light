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
* An idle session stops warming (#319). After MAX_IDLE_WARMS warms with no
  turn, a forgotten terminal sends nothing more; at the /budget cap no warm is
  sent; and once the entry has expired (the machine slept past its hour) a
  warm would be a full 1-hour write, so the next turn writes it instead.

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

    def __init__(self, start, end, on_wait=None):
        self.now = start
        self.end = end
        self.on_wait = on_wait

    def time(self):
        return self.now

    def wait(self, timeout=None):
        self.now += timeout
        if self.on_wait is not None:
            self.on_wait(self)
        return self.is_set()

    def is_set(self):
        return self.now >= self.end


def _run_heartbeat(idle_seconds, last_turn_ago=0.0, sleep=None, turns=()):
    """Run the real heartbeat over `idle_seconds` of fake time after a turn
    `last_turn_ago` seconds before the start; return the times it warmed.

    `sleep` is (at, duration): the machine sleeps once `at` seconds in, and the
    next tick comes `duration` seconds later. `turns` are the times, in seconds
    from the start, at which the user asks a question."""
    start = 1_000_000.0
    pending_turns = sorted(turns)
    slept = []

    def on_wait(clock):
        if sleep is not None and not slept and clock.now - start >= sleep[0]:
            slept.append(True)
            clock.now += sleep[1]
        while pending_turns and clock.now - start >= pending_turns[0]:
            pending_turns.pop(0)
            state.turn_count += 1
            state.last_interaction = clock.now
            llm_light.last_interaction = clock.now

    clock = _FakeClock(start, start + idle_seconds, on_wait)
    warms = []

    def fake_warm(quiet=False):
        warms.append(clock.now)

    # Once any wrapped call has run, main.heartbeat is llm_light's wrapper,
    # whose _sync_bindings() copies the package-level stop_event,
    # last_interaction and warm_cache into state, main and llm. Patch both
    # layers, or the heartbeat waits on the real stop event forever.
    saved = (state.stop_event, state.last_interaction, state.turn_count)
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
        state.stop_event, state.last_interaction, state.turn_count = saved
    return [w - start for w in warms], -last_turn_ago


class TestHeartbeatPacing(unittest.TestCase):

    def test_an_idle_hour_costs_at_most_one_warm(self):
        warms, _ = _run_heartbeat(ONE_HOUR)
        self.assertLessEqual(len(warms), 1, f"warmed {len(warms)} times in one idle hour")

    def test_the_entry_is_refreshed_before_its_ttl_runs_out(self):
        # Within the idle cap, no gap between cache uses may reach the TTL, or
        # the next turn pays a fresh 1-hour write.
        idle = 2 * ONE_HOUR
        warms, last_turn = _run_heartbeat(idle)
        uses = [last_turn] + warms
        gaps = [b - a for a, b in zip(uses, uses[1:] + [idle])]
        self.assertTrue(warms, "never warmed in two idle hours")
        self.assertLess(max(gaps), ONE_HOUR, f"cache uses at {uses}")

    def test_three_idle_hours_need_three_warms_at_most(self):
        warms, _ = _run_heartbeat(3 * ONE_HOUR)
        self.assertLessEqual(len(warms), 3)

    def test_a_recent_turn_postpones_the_warm(self):
        # The turn itself read the entry, which reset its timer.
        warms, _ = _run_heartbeat(10 * 60, last_turn_ago=0)
        self.assertEqual(warms, [])

    def test_a_forgotten_terminal_stops_warming(self):
        # A weekend with the session open: without a cap it warmed every 55
        # minutes, about 65 paid requests over 60 hours (#319).
        warms, _ = _run_heartbeat(60 * ONE_HOUR)
        self.assertEqual(len(warms), config.MAX_IDLE_WARMS, f"warms at {warms}")

    def test_a_turn_resets_the_idle_cap(self):
        # The user came back: the next idle stretch gets its own warms.
        back_at = 30 * ONE_HOUR
        warms, _ = _run_heartbeat(60 * ONE_HOUR, turns=[back_at])
        after = [w for w in warms if w > back_at]
        self.assertEqual(len(after), config.MAX_IDLE_WARMS, f"warms at {warms}")

    def test_no_warm_once_the_entry_has_expired(self):
        # The machine slept past the entry's hour: a warm on waking would be a
        # 1-hour write (twice the input price), paid even if the user just quits.
        warms, _ = _run_heartbeat(4 * ONE_HOUR, sleep=(30 * 60, 2 * ONE_HOUR))
        self.assertEqual(warms, [])

    def test_a_turn_after_the_expiry_warms_again(self):
        # The turn wrote a fresh entry; that one is worth keeping alive.
        warms, _ = _run_heartbeat(5 * ONE_HOUR, sleep=(30 * 60, 2 * ONE_HOUR),
                                  turns=[3 * ONE_HOUR])
        self.assertTrue(warms, "never warmed after the turn that followed the sleep")
        self.assertGreater(warms[0], 3 * ONE_HOUR)


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


class TestWarmHonoursTheBudget(unittest.TestCase):
    """A warm is spend the user did not ask for: at the /budget cap it is not sent (#319)."""

    def setUp(self):
        self._saved = (state.session_model, state.pinned_model,
                       state.session_budget, state.session_cost)
        state.session_model = config.MODEL_SONNET
        state.pinned_model = None

    def tearDown(self):
        (state.session_model, state.pinned_model,
         state.session_budget, state.session_cost) = self._saved

    def _requests(self):
        resp = MagicMock()
        resp.usage = _usage()
        # The wrapper's _sync_bindings() copies the package's session_cost into
        # state, so set both layers.
        with patch("llm_light.providers.claude.client.messages.create", return_value=resp) as create, \
             patch.object(llm_light, "session_cost", state.session_cost):
            llm_light.warm_cache(quiet=True)
        return create.call_count

    def test_no_warm_at_the_cap(self):
        state.session_budget = 1.00
        state.session_cost = 1.00
        self.assertEqual(self._requests(), 0)

    def test_no_warm_past_the_cap(self):
        state.session_budget = 1.00
        state.session_cost = 1.25
        self.assertEqual(self._requests(), 0)

    def test_warms_under_the_cap(self):
        state.session_budget = 1.00
        state.session_cost = 0.50
        self.assertEqual(self._requests(), 1)

    def test_warms_without_a_budget(self):
        state.session_budget = None
        state.session_cost = 5.00
        self.assertEqual(self._requests(), 1)


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


class TestWatcherSendsNoWarm(unittest.TestCase):
    """A skeleton change from the file watcher rebuilds the skeleton and sends
    nothing. A warm there is never cheaper than letting the next turn write the
    entry: warm-then-turn costs one 1-hour write plus a read, turn alone costs
    the write. Every further change before that turn (a new file, an editor's
    rename-on-save, a deleted temp file) was another full 1-hour write."""

    def setUp(self):
        self._saved = (state.skeleton_context, state.session_model)
        state.session_model = config.MODEL_SONNET   # a model a warm would target

    def tearDown(self):
        state.skeleton_context, state.session_model = self._saved

    def _requests(self, refresh):
        # Through llm_light's wrappers, whose _sync_bindings() puts the real
        # warm_cache back into llm (see TestWarmModel._warmed_models).
        resp = MagicMock()
        resp.usage = _usage()
        with patch("llm_light.providers.claude.client.messages.create", return_value=resp) as create, \
             patch.object(llm, "_refresh_tree_only", return_value="tree v2"), \
             patch.object(llm, "_refresh_single_md", return_value=True), \
             patch.object(llm, "_assemble_skeleton", return_value="skeleton v2"):
            refresh()
        return create.call_count

    def test_a_new_or_deleted_file_sends_no_request(self):
        self.assertEqual(self._requests(llm_light.refresh_skeleton_only), 0)
        self.assertEqual(state.skeleton_context, "tree v2")

    def test_a_changed_md_file_sends_no_request(self):
        with patch.object(llm, "notify"):
            self.assertEqual(self._requests(lambda: llm_light.refresh_md_file("README.md")), 0)
        self.assertEqual(state.skeleton_context, "skeleton v2")


if __name__ == "__main__":
    unittest.main()
