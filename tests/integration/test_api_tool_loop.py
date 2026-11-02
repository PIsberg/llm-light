"""
Integration test: the API-key tool loop through the real anthropic SDK.

A local HTTP server stands in for the Messages streaming endpoint, so this
runs offline and needs no API key. It exercises what mocks cannot: that the
SDK accepts the tool definitions, streams a tool_use, lets the loop pass the
assistant content back unchanged, and serialises the tool_result.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy-test-key")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from llm_light.providers import claude as _claude_mod  # noqa: E402  (T017: moved transport)


def _sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def _start(n_in):
    return {"type": "message_start", "message": {
        "id": f"msg_{n_in}", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": n_in, "output_tokens": 1,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}}


_TOOL_TURN = [
    _start(100),
    {"type": "content_block_start", "index": 0,
     "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {}}},
    {"type": "content_block_delta", "index": 0,
     "delta": {"type": "input_json_delta", "partial_json": json.dumps({"path": "pkg/retry.py"})}},
    {"type": "content_block_stop", "index": 0},
    {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
     "usage": {"output_tokens": 20}},
    {"type": "message_stop"},
]
_ANSWER_TURN = [
    _start(300),
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0,
     "delta": {"type": "text_delta", "text": "It is retry_with_backoff in pkg/retry.py."}},
    {"type": "content_block_stop", "index": 0},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
     "usage": {"output_tokens": 15}},
    {"type": "message_stop"},
]


class _StubMessagesAPI(BaseHTTPRequestHandler):
    requests = []
    headers_seen = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append(body)
        type(self).headers_seen.append(dict(self.headers))
        data = _sse(_TOOL_TURN if len(type(self).requests) == 1 else _ANSWER_TURN)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class TestApiToolLoopThroughSdk(unittest.TestCase):

    def setUp(self):
        self.project = tempfile.mkdtemp(prefix="cl_project_")
        Path(self.project, "pkg").mkdir()
        Path(self.project, "pkg", "retry.py").write_text(
            "def retry_with_backoff(fn):\n    return fn\n", encoding="utf-8")
        self.cwd = os.getcwd()
        os.chdir(self.project)
        _StubMessagesAPI.requests = []
        _StubMessagesAPI.headers_seen = []
        self.server = HTTPServer(("127.0.0.1", 0), _StubMessagesAPI)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        os.chdir(self.cwd)
        shutil.rmtree(self.project, ignore_errors=True)

    def test_read_file_round_trip(self):
        print("\n  ▶ TestApiToolLoopThroughSdk.test_read_file_round_trip")
        import io
        import anthropic
        import llm_light.config as config
        from llm_light import llm

        stub = anthropic.Anthropic(api_key="sk-ant-stub",
                                   base_url=f"http://127.0.0.1:{self.server.server_port}")
        kwargs = dict(model="claude-sonnet-5-5", max_tokens=2000,
                      system=llm._build_system_blocks("SKELETON"),
                      messages=[{"role": "user", "content": [{"type": "text", "text": "Where is retry?"}]}])
        llm._apply_effort(kwargs, "medium")
        with patch.object(config, "AUTH_MODE", "API_KEY"), \
             patch.object(_claude_mod, "client", stub), \
             patch("sys.stdout", io.StringIO()):
            reply, usage, _ = _claude_mod._make_streaming_api_call(**kwargs)

        reqs = _StubMessagesAPI.requests
        self.assertEqual(len(reqs), 2)
        self.assertEqual({t["name"] for t in reqs[0]["tools"]},
                         {"read_file", "fetch_chunk", "grep", "list_files", "shell"})
        assistant, results = reqs[1]["messages"][-2], reqs[1]["messages"][-1]
        self.assertEqual(assistant["content"][0]["type"], "tool_use")
        self.assertEqual(assistant["content"][0]["id"], "toolu_1")
        result = results["content"][0]
        self.assertEqual(result["tool_use_id"], "toolu_1")
        self.assertIn("1\tdef retry_with_backoff(fn):", result["content"])
        self.assertEqual(reply, "It is retry_with_backoff in pkg/retry.py.")
        self.assertEqual((usage.input_tokens, usage.output_tokens), (400, 35))

    def test_effort_message_and_beta_reach_the_wire(self):
        # The SDK's types have no role "system" message; check it is sent as
        # built, with the beta header, on every round of the tool loop.
        print("\n  ▶ TestApiToolLoopThroughSdk.test_effort_message_and_beta_reach_the_wire")
        import io
        import anthropic
        import llm_light.config as config
        from llm_light import llm

        stub = anthropic.Anthropic(api_key="sk-ant-stub",
                                   base_url=f"http://127.0.0.1:{self.server.server_port}")
        kwargs = dict(model="claude-sonnet-5-5", max_tokens=2000,
                      system=llm._build_system_blocks("SKELETON"),
                      messages=[{"role": "user", "content": [{"type": "text", "text": "Where is retry?"}]}],
                      extra_headers=llm._EXTENDED_CACHE_HEADERS)
        llm._apply_effort(kwargs, "low")
        with patch.object(config, "AUTH_MODE", "API_KEY"), \
             patch.object(_claude_mod, "client", stub), \
             patch("sys.stdout", io.StringIO()):
            _claude_mod._make_streaming_api_call(**kwargs)

        effort_msg = {"role": "system", "content": [], "output_config": {"effort": "low"}}
        for body, headers in zip(_StubMessagesAPI.requests, _StubMessagesAPI.headers_seen):
            self.assertEqual(body["output_config"], {"effort": llm.BASE_EFFORT})
            self.assertEqual(body["messages"][0], effort_msg)
            self.assertIn(llm.EFFORT_BETA, headers["anthropic-beta"])
        self.assertEqual(len(_StubMessagesAPI.requests), 2)

    def test_no_request_forces_a_tool_on_any_routed_model(self):
        # Fable 5.1, Opus 5.5 and Sonnet 5.5 answer 400 to tool_choice any or
        # tool (#92), so every round on every model must leave it out.
        print("\n  ▶ TestApiToolLoopThroughSdk.test_no_request_forces_a_tool_on_any_routed_model")
        import io
        import anthropic
        import llm_light.config as config
        from llm_light import llm

        stub = anthropic.Anthropic(api_key="sk-ant-stub",
                                   base_url=f"http://127.0.0.1:{self.server.server_port}")
        for model in config.MODEL_PRICES:
            _StubMessagesAPI.requests = []
            kwargs = dict(model=model, max_tokens=2000,
                          system=llm._build_system_blocks("SKELETON"),
                          messages=[{"role": "user", "content": [{"type": "text", "text": "Where is retry?"}]}],
                          extra_headers=llm._EXTENDED_CACHE_HEADERS)
            llm._apply_effort(kwargs, "medium")
            with patch.object(config, "AUTH_MODE", "API_KEY"), \
                 patch.object(_claude_mod, "client", stub), \
                 patch("sys.stdout", io.StringIO()):
                _claude_mod._make_streaming_api_call(**kwargs)
            self.assertEqual(len(_StubMessagesAPI.requests), 2, model)
            for body in _StubMessagesAPI.requests:
                self.assertEqual(body["model"], model)
                self.assertTrue(body["tools"], model)
                self.assertNotIn("tool_choice", body, model)


_RETENTION_MESSAGE = "claude-fable-5-1 requires 30-day data retention."


class _StubRefusesWith400(BaseHTTPRequestHandler):
    """The shape of the 400 a zero-data-retention org gets on Fable 5.1 (#92). The
    wording is ours: no such org was available, so only the shape is real."""
    hits = 0

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        type(self).hits += 1
        data = json.dumps({"type": "error", "error": {"type": "invalid_request_error",
                                                      "message": _RETENTION_MESSAGE},
                           "request_id": "req_stub_400"}).encode()
        self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.send_header("request-id", "req_stub_400")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class TestApiErrorIsReadable(unittest.TestCase):

    def setUp(self):
        self.project = tempfile.mkdtemp(prefix="cl_project_")
        self.cwd = os.getcwd()
        os.chdir(self.project)
        _StubRefusesWith400.hits = 0
        self.server = HTTPServer(("127.0.0.1", 0), _StubRefusesWith400)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        os.chdir(self.cwd)
        shutil.rmtree(self.project, ignore_errors=True)

    def test_a_400_prints_the_apis_own_words_once_and_no_trace(self):
        print("\n  ▶ TestApiErrorIsReadable.test_a_400_prints_the_apis_own_words_once_and_no_trace")
        import contextlib
        import io
        import anthropic
        import llm_light.config as config
        from llm_light import llm

        stub = anthropic.Anthropic(api_key="sk-ant-stub",
                                   base_url=f"http://127.0.0.1:{self.server.server_port}")
        out = io.StringIO()
        with patch.object(config, "AUTH_MODE", "API_KEY"), \
             patch.object(_claude_mod, "client", stub), \
             patch.object(llm, "route_query", return_value=(config.MODEL_FABLE, "medium", 2000)), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            llm.chat("Where is retry?", use_index=False)  # must not raise

        printed = out.getvalue()
        self.assertIn(_RETENTION_MESSAGE, printed)
        self.assertIn("400", printed)
        self.assertIn("req_stub_400", printed)  # what support asks for
        self.assertNotIn("Traceback", printed)
        self.assertNotIn("{'type'", printed)  # not the SDK's dict repr
        self.assertEqual(_StubRefusesWith400.hits, 1)  # a 400 is not retried


if __name__ == "__main__":
    unittest.main()
