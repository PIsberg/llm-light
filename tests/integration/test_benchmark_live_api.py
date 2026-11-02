"""Tests for tests/benchmarks/benchmark_live_api.py.

The benchmark bills an API key, so these run it through the real anthropic SDK
against a local stub of the Messages endpoint. They check that it refuses to start
without a real key and an explicit go-ahead, that each experiment puts the request
its issue asks about on the wire, and that the numbers it reports are the API's.
"""
import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

import llm_light.config as config
from llm_light import llm, state, tools
from llm_light.providers import claude as claude_mod

_BENCH = Path(__file__).resolve().parents[1] / "benchmarks" / "benchmark_live_api.py"
_REAL_KEY = "sk-ant-api03-stub"


@pytest.fixture
def bench():
    spec = importlib.util.spec_from_file_location("benchmark_live_api", _BENCH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def _answer(n):
    """A streamed answer. cache_read grows with n, so a test can tell the turns apart."""
    return [
        {"type": "message_start", "message": {
            "id": f"msg_{n}", "type": "message", "role": "assistant", "model": "m",
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 1,
                      "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000 * n}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "PONG"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
         "usage": {"output_tokens": 5}},
        {"type": "message_stop"},
    ]


class _Stub(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append(body)
        n = len(type(self).requests)
        if body.get("stream"):
            data, kind = _sse(_answer(n)), "text/event-stream"
        else:
            data = json.dumps({
                "id": f"msg_{n}", "type": "message", "role": "assistant", "model": body["model"],
                "content": [{"type": "text", "text": "PONG"}], "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 5,
                          "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}).encode()
            kind = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def stub_api(tmp_path, monkeypatch):
    """The real SDK pointed at the stub, in a small project, with clean session state."""
    import anthropic
    project = tmp_path / "project"
    project.mkdir()
    (project / "retry.py").write_text("def retry_with_backoff(fn):\n    return fn\n", encoding="utf-8")
    monkeypatch.chdir(project)
    _Stub.requests = []
    server = HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = anthropic.Anthropic(api_key="sk-ant-stub",
                                 base_url=f"http://127.0.0.1:{server.server_port}")
    saved = (list(state.conversation_history), state.turn_count, state.session_cost,
             state.pinned_model, state.skeleton_context)
    with patch.object(config, "AUTH_MODE", "API_KEY"), patch.object(claude_mod, "client", client):
        yield _Stub.requests
    state.conversation_history[:] = saved[0]
    state.turn_count, state.session_cost, state.pinned_model, state.skeleton_context = saved[1:]
    server.shutdown()
    server.server_close()


def _run(bench, tmp_path, *argv, key=_REAL_KEY):
    out = tmp_path / "live.json"
    with patch.object(bench.config, "API_KEY", key):
        code = bench.main(["--out", str(out), "--i-accept-the-bill", *argv])
    return code, json.loads(out.read_text(encoding="utf-8")) if out.exists() else None


def _effort(body):
    """The turn's routed effort: the effort-only message, or the top-level one."""
    for message in body["messages"]:
        if message.get("role") == "system" and "output_config" in message:
            return message["output_config"]["effort"]
    return body["output_config"]["effort"]


# --- it spends money only when told to, and only on a real key -------------------

def test_refuses_without_the_explicit_go_ahead(bench, stub_api, tmp_path, capsys):
    with patch.object(bench.config, "API_KEY", _REAL_KEY):
        assert bench.main(["--out", str(tmp_path / "x.json")]) == 2
    assert stub_api == []
    assert "--i-accept-the-bill" in capsys.readouterr().err


def test_refuses_a_subscription_token_used_as_a_key(bench, stub_api, tmp_path, capsys):
    code, _ = _run(bench, tmp_path, "--experiment", "sdk", key="sk-ant-oat01-stub")
    assert code == 2
    assert stub_api == []
    assert "subscription" in capsys.readouterr().err


def test_refuses_in_oauth_mode(bench, stub_api, tmp_path, capsys):
    with patch.object(config, "AUTH_MODE", "OAUTH"):
        code, _ = _run(bench, tmp_path, "--experiment", "sdk")
    assert code == 2
    assert stub_api == []
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


# --- what each experiment puts on the wire ----------------------------------------

def test_sdk_sends_one_create_and_one_stream_on_haiku(bench, stub_api, tmp_path):
    code, report = _run(bench, tmp_path, "--experiment", "sdk")
    assert code == 0
    assert [b["model"] for b in stub_api] == [config.MODEL_HAIKU] * 2
    assert [bool(b.get("stream")) for b in stub_api] == [False, True]
    for body in stub_api:
        assert body["output_config"] == {"effort": llm.BASE_EFFORT}
        assert any("cache_control" in block for block in body["system"])
    assert [r["call"] for r in report["sdk"]] == ["messages.create", "messages.stream"]


def test_effort_runs_the_issues_sequence_through_chat(bench, stub_api, tmp_path):
    code, report = _run(bench, tmp_path, "--experiment", "effort")
    assert code == 0
    assert [(b["model"], _effort(b)) for b in stub_api] == [
        (config.MODEL_SONNET, "medium"), (config.MODEL_SONNET, "high"),
        (config.MODEL_HAIKU, "low"), (config.MODEL_SONNET, "medium")]
    # The top level never moves: that is the claim under test (#88).
    assert {b["output_config"]["effort"] for b in stub_api} == {llm.BASE_EFFORT}
    # A real conversation: each turn carries the one before it.
    assert len(stub_api[1]["messages"]) > len(stub_api[0]["messages"])
    # The API's own usage, turn by turn (the stub's cache_read is 1000 * n).
    assert [r["cache_read"] for r in report["effort"]] == [1000, 2000, 3000, 4000]
    assert report["effort"][0]["cache_write"] == 100


def test_effort_in_prefix_moves_the_effort_to_the_top_level(bench, stub_api, tmp_path):
    # #88 question 3: does a top-level change invalidate the cached skeleton?
    code, _ = _run(bench, tmp_path, "--experiment", "effort", "--effort-in-prefix")
    assert code == 0
    assert [b["output_config"]["effort"] for b in stub_api] == ["medium", "high", "low", "medium"]
    for body in stub_api:
        assert not [m for m in body["messages"] if m.get("role") == "system"]


def test_each_run_salts_the_skeleton_so_it_starts_cold(bench, stub_api, tmp_path):
    # Otherwise the second arm of #88 reads what the first one wrote.
    _run(bench, tmp_path, "--experiment", "effort")
    first = stub_api[0]["system"]
    assert "retry_with_backoff" in json.dumps(first) or "retry.py" in json.dumps(first)
    stub_api.clear()
    _run(bench, tmp_path, "--experiment", "effort")
    assert stub_api[0]["system"] != first


def test_toptier_pins_fable_then_opus(bench, stub_api, tmp_path):
    code, report = _run(bench, tmp_path, "--experiment", "toptier")
    assert code == 0
    assert [b["model"] for b in stub_api] == [config.MODEL_FABLE] * 2 + [config.MODEL_OPUS] * 2
    assert all("tool_choice" not in b for b in stub_api)
    # Priced with MODEL_PRICES, so the run can be compared with the Console.
    row = report["toptier"][0]
    prices = config.MODEL_PRICES[config.MODEL_FABLE]
    assert row["usd"] == pytest.approx((10 * prices["input"] + 100 * prices["write"]
                                        + 1000 * prices["read"] + 5 * prices["output"]) / 1e6)


def test_fetch_chunk_arms_differ_only_in_that_tool(bench, stub_api, tmp_path):
    before = list(tools.TOOLS)
    # No embedding model here; the arms are about what reaches the wire.
    with patch.object(bench, "_build_index", return_value=False):
        code, report = _run(bench, tmp_path, "--experiment", "fetch-chunk", "--limit", "2")
    assert code == 0
    assert tools.TOOLS == before  # the arm is restored afterwards
    names = [{t["name"] for t in b["tools"]} for b in stub_api]
    assert names[:2] == [{t["name"] for t in before}] * 2
    assert names[2:] == [{t["name"] for t in before} - {"fetch_chunk"}] * 2
    # Questions are independent: no history leaks from one to the next.
    assert len(stub_api[0]["messages"]) == len(stub_api[1]["messages"])
    assert {r["arm"] for r in report["fetch-chunk"]} == {"with", "without"}


def test_stops_at_the_spend_cap(bench, stub_api, tmp_path, capsys):
    code, report = _run(bench, tmp_path, "--experiment", "effort", "--max-usd", "0.0000001")
    assert code == 1
    assert len(report["effort"]) == 1
    assert "cap" in capsys.readouterr().err


def test_a_refused_turn_is_recorded_not_reported_as_a_pass(bench, stub_api, tmp_path):
    # chat() prints a failed turn and returns, so a refusal would otherwise look
    # like a turn that happened to report no tokens.
    from llm_light.providers.base import ProviderError
    with patch.object(claude_mod.PROVIDER, "complete",
                      side_effect=ProviderError("Claude API error 400: no")):
        code, report = _run(bench, tmp_path, "--experiment", "effort")
    assert code == 1
    assert report["effort"][0]["error"] == "Claude API error 400: no"
