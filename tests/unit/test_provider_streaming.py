"""agy's reply streams into the view as it arrives (#328).

run_cli() waited for the CLI to exit (communicate()) behind a spinner, so an agy
reply appeared all at once after 15 to 120 seconds, and a -p stream-json run sent no
text events until the end. agy's stream-json output carries the reply as
step_update text_delta events of step_type agent_response, which join up to the
result's response (checked on agy 1.5.14). codex exec --json sends its reply in one
item.completed event, so there is nothing earlier to show; codex keeps its spinner.
"""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import llm_light  # noqa: F401
from llm_light import state
from llm_light.providers import gemini, runner
from llm_light.providers.base import ProviderError

# --- run_cli(on_line=...) -----------------------------------------------------------

_WAITS_FOR_MARKER = """
import os, sys, time
marker = sys.argv[1]
print("first", flush=True)
deadline = time.monotonic() + 10
while not os.path.exists(marker) and time.monotonic() < deadline:
    time.sleep(0.05)
print("second" if os.path.exists(marker) else "never streamed", flush=True)
"""


def test_lines_reach_the_callback_while_the_cli_still_runs(tmp_path):
    # The child prints "second" only after the callback saw "first": a run_cli that
    # waits for the child to exit would see "never streamed" after 10 seconds.
    marker = tmp_path / "seen-first"
    seen = []

    def on_line(line):
        seen.append(line.strip())
        if line.strip() == "first":
            marker.write_text("x")

    start = time.monotonic()
    out = runner.run_cli("test", [sys.executable, "-c", _WAITS_FOR_MARKER, str(marker)], "",
                         on_line=on_line)
    assert seen == ["first", "second"]
    assert out.split() == ["first", "second"]
    assert time.monotonic() - start < 8


def test_a_streamed_run_that_fails_names_the_error():
    script = "import sys; print('partial', flush=True); sys.stderr.write('Error: boom\\n'); sys.exit(3)"
    with pytest.raises(ProviderError, match=r"exit 3.*Error: boom"):
        runner.run_cli("test", [sys.executable, "-c", script], "", on_line=lambda line: None)


def test_a_streamed_run_that_hangs_times_out():
    script = "import time; print('x', flush=True); time.sleep(30)"
    start = time.monotonic()
    with pytest.raises(ProviderError, match="timed out"):
        runner.run_cli("test", [sys.executable, "-c", script], "", timeout=1, on_line=lambda line: None)
    assert time.monotonic() - start < 10


def test_a_streamed_run_reads_all_of_a_large_stdin():
    script = "import sys; data = sys.stdin.read(); print(len(data))"
    out = runner.run_cli("test", [sys.executable, "-c", script], "x" * 300_000, on_line=lambda line: None)
    assert out.strip() == "300000"


# --- GeminiProvider.complete ------------------------------------------------------------

def _delta(i, text, step_type="agent_response"):
    return {"event": "step_update", "step_update": {"conversation_id": "c", "step_index": i,
                                                    "state": "ACTIVE", "step_type": step_type,
                                                    "text_delta": text}}


def _stream(*events):
    return [json.dumps(e) + "\n" for e in events]


def _fake_run(lines):
    def run_cli(provider, argv, stdin_text, timeout=None, cwd=None, env=None, on_line=None):
        for line in lines:
            if on_line is not None:
                on_line(line)
        return "".join(lines)
    return run_cli


def _request():
    return {"system": [{"type": "text", "text": "SYS"}],
            "messages": [{"role": "user", "content": "the question"}]}


@pytest.fixture
def sink():
    events = []
    saved = state.event_sink
    state.event_sink = events.append
    yield events
    state.event_sink = saved


def _complete(lines):
    with patch("llm_light.providers.gemini.shutil.which", return_value="/bin/agy"), \
         patch("llm_light.providers.gemini.run_cli", side_effect=_fake_run(lines)):
        return gemini.PROVIDER.complete(_request())


def test_the_reply_streams_as_its_deltas_arrive(sink):
    lines = _stream({"event": "init"},
                    {"event": "step_update", "step_update": {"step_type": "user_input", "state": "DONE"}},
                    _delta(1, "Blue is "), _delta(1, "calm."),
                    {"event": "result", "result": {"status": "SUCCESS", "response": "Blue is calm."}})
    reply, _usage, shown = _complete(lines)
    assert reply == "Blue is calm."
    assert shown is True                                # chat() must not print it again
    assert [e["text"] for e in sink if e["type"] == "text"] == ["Blue is ", "calm."]


def test_other_step_types_are_not_shown(sink):
    lines = _stream(_delta(0, "SECRET PLAN", step_type="planner_thought"), _delta(1, "Answer."),
                    {"event": "result", "result": {"status": "SUCCESS", "response": "Answer."}})
    _complete(lines)
    assert "SECRET PLAN" not in "".join(e.get("text", "") for e in sink)


def test_a_reply_without_deltas_is_still_shown_once(sink):
    # An agy release that stops sending deltas must not lose the reply on screen.
    lines = _stream({"event": "result", "result": {"status": "SUCCESS", "response": "Whole reply."}})
    reply, _usage, shown = _complete(lines)
    assert shown is True
    assert "".join(e["text"] for e in sink if e["type"] == "text") == "Whole reply."


def test_a_forbidden_tool_still_fails_the_turn(sink):
    lines = _stream(_delta(1, "Done."),
                    {"event": "step_update", "step_update": {"step_type": "tool", "state": "DONE",
                                                             "tool_name": "run_command"}},
                    {"event": "result", "result": {"status": "SUCCESS", "response": "Done."}})
    with pytest.raises(ProviderError, match="forbids"):
        _complete(lines)
