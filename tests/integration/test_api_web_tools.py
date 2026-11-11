"""
Integration test: --web in API-key mode, through the real anthropic SDK (#231).

A local HTTP server stands in for the Messages streaming endpoint, as in
test_api_tool_loop.py, and streams the blocks a server-run web search
produces: server_tool_use, web_search_tool_result, and a pause_turn stop. It
checks what a mock cannot: that the SDK accepts the server tool definitions,
parses those blocks, and that the loop sends them back unchanged.
"""

import io
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy-test-key")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from llm_light.providers import claude as _claude_mod  # noqa: E402


def _sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def _start(n_in):
    return {"type": "message_start", "message": {
        "id": f"msg_{n_in}", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": n_in, "output_tokens": 1,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}}


def _search(index, query, result):
    return [
        {"type": "content_block_start", "index": index, "content_block": {
            "type": "server_tool_use", "id": f"srvtoolu_{index}", "name": "web_search", "input": {}}},
        {"type": "content_block_delta", "index": index,
         "delta": {"type": "input_json_delta", "partial_json": json.dumps({"query": query})}},
        {"type": "content_block_stop", "index": index},
        {"type": "content_block_start", "index": index + 1, "content_block": {
            "type": "web_search_tool_result", "tool_use_id": f"srvtoolu_{index}", "content": result}},
        {"type": "content_block_stop", "index": index + 1},
    ]


def _text(index, text):
    return [
        {"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": index},
    ]


def _end(stop_reason, searches):
    return [
        {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
         "usage": {"output_tokens": 20, "server_tool_use": {"web_search_requests": searches}}},
        {"type": "message_stop"},
    ]


_HIT = [{"type": "web_search_result", "url": "https://example.org/release",
         "title": "Release notes", "encrypted_content": "Zm9v", "page_age": None}]
_ERROR = {"type": "web_search_tool_result_error", "error_code": "max_uses_exceeded"}

SCENARIOS = {
    # One search, then the answer, in one response.
    "search": [[_start(100), *_search(0, "llm-light release date", _HIT),
                *_text(2, "It shipped in November."), *_end("end_turn", 1)]],
    # The server loop paused after a search; the client resends, and it finishes.
    "pause": [[_start(100), *_search(0, "first query", _HIT), *_end("pause_turn", 1)],
              [_start(400), *_search(0, "second query", _HIT),
               *_text(2, "Done after the pause."), *_end("end_turn", 1)]],
    # A failed search comes back as a 200 with an error object in the result block.
    "error": [[_start(100), *_search(0, "too many", _ERROR),
               *_text(2, "I could not search."), *_end("end_turn", 0)]],
}


class _Stub(BaseHTTPRequestHandler):
    scenario = "search"
    requests = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append(body)
        turns = SCENARIOS[type(self).scenario]
        data = _sse(turns[min(len(type(self).requests), len(turns)) - 1])
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def stub(tmp_path, monkeypatch):
    import anthropic
    import llm_light.config as config
    monkeypatch.chdir(tmp_path)
    _Stub.requests = []
    server = HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = anthropic.Anthropic(api_key="sk-ant-stub", base_url=f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setattr(config, "AUTH_MODE", "API_KEY")
    monkeypatch.setattr(config, "WEB_ACCESS", True)
    monkeypatch.setattr(_claude_mod, "client", client)
    yield _Stub
    server.shutdown()
    server.server_close()


def _turn(scenario, model="claude-sonnet-5-5"):
    from llm_light import llm
    _Stub.scenario = scenario
    kwargs = dict(model=model, max_tokens=2000, system=llm._build_system_blocks("SKELETON"),
                  messages=[{"role": "user", "content": [{"type": "text", "text": "When was it released?"}]}])
    llm._apply_effort(kwargs, "medium")
    out, err = io.StringIO(), io.StringIO()
    with patch("sys.stdout", out), patch("sys.stderr", err):  # tool lines go to stderr off a tty
        reply, usage, _ = _claude_mod._make_streaming_api_call(**kwargs)
    return reply, usage, out.getvalue() + err.getvalue()


def test_a_search_turn_answers_and_counts_the_search(stub):
    reply, usage, shown = _turn("search")
    assert reply == "It shipped in November."
    assert len(stub.requests) == 1
    assert [t["type"] for t in stub.requests[0]["tools"] if "type" in t] == [
        "web_search_20260209", "web_fetch_20260209"]
    assert usage.server_tool_use.web_search_requests == 1
    assert "llm-light release date" in shown  # the search is shown as it happens


def test_haiku_gets_the_basic_tool_versions(stub):
    _turn("search", model="claude-haiku-5-5")
    assert [t["type"] for t in stub.requests[0]["tools"] if "type" in t] == [
        "web_search_20250305", "web_fetch_20250910"]


def test_pause_turn_resends_the_paused_content_and_no_new_question(stub):
    reply, usage, _ = _turn("pause")
    assert reply == "Done after the pause."
    first, second = stub.requests
    assert second["messages"][:-1] == first["messages"]
    resumed = second["messages"][-1]
    assert resumed["role"] == "assistant"
    assert [b["type"] for b in resumed["content"]] == ["server_tool_use", "web_search_tool_result"]
    assert resumed["content"][0]["input"] == {"query": "first query"}
    assert usage.server_tool_use.web_search_requests == 2


def test_a_failed_search_is_shown_and_the_turn_still_answers(stub):
    reply, _, shown = _turn("error")
    assert reply == "I could not search."
    assert "max_uses_exceeded" in shown
