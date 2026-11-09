"""
Integration test: MCP servers in API-key mode, through the real mcp SDK.

One real stdio server (tests/fixtures/mcp_nonce_server.py) is started for the module,
since starting one costs seconds. Its `nonce` tool returns a value only that process
knows, so a result that carries it proves the call reached the server. The last test
runs a whole API-key turn: a stub Messages endpoint asks for the MCP tool, and the
tool_result the real anthropic SDK sends back must carry the nonce.
"""

import io
import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy-test-key")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from llm_light import mcp_servers, permissions, tools  # noqa: E402
from llm_light.providers import claude as _claude_mod  # noqa: E402

_SERVER = Path(__file__).resolve().parent.parent / "fixtures" / "mcp_nonce_server.py"
_NONCE = "K4-PELICAN-0731"


def _stdio(*args):
    return {"type": "stdio", "command": sys.executable, "args": [str(_SERVER), *args], "env": {}}


def setUpModule():
    mcp_servers.stop()
    lines = mcp_servers.start({"nonce": _stdio(_NONCE)}, timeout=110)
    if not mcp_servers.tool_definitions():
        raise RuntimeError("the fixture server did not start: " + "; ".join(lines))


def tearDownModule():
    mcp_servers.stop()


class TestMcpStdio(unittest.TestCase):

    def setUp(self):
        permissions.set_mode(permissions.DEFAULT)

    def test_tools_are_listed_with_their_schemas(self):
        print("\n  ▶ TestMcpStdio.test_tools_are_listed_with_their_schemas")
        defs = {d["name"]: d for d in mcp_servers.tool_definitions()}
        self.assertEqual(set(defs), {"mcp__nonce__nonce", "mcp__nonce__fail",
                                     "mcp__nonce__big", "mcp__nonce__picture"})
        nonce = defs["mcp__nonce__nonce"]
        self.assertEqual(nonce["input_schema"]["type"], "object")
        self.assertIn("label", nonce["input_schema"]["properties"])
        self.assertIn("secret nonce", nonce["description"])

    def test_a_call_reaches_the_server(self):
        print("\n  ▶ TestMcpStdio.test_a_call_reaches_the_server")
        self.assertEqual(tools.run_tool("mcp__nonce__nonce", {"label": "L-"}), ("L-" + _NONCE, False))

    def test_an_error_result_is_an_error_for_the_model(self):
        print("\n  ▶ TestMcpStdio.test_an_error_result_is_an_error_for_the_model")
        text, is_error = tools.run_tool("mcp__nonce__fail", {})
        self.assertTrue(is_error)
        self.assertIn("fail", text)

    def test_a_large_result_is_capped(self):
        print("\n  ▶ TestMcpStdio.test_a_large_result_is_capped")
        text, is_error = tools.run_tool("mcp__nonce__big", {})
        self.assertFalse(is_error)
        self.assertLess(len(text), 41_000)

    def test_an_image_result_is_described(self):
        print("\n  ▶ TestMcpStdio.test_an_image_result_is_described")
        self.assertEqual(tools.run_tool("mcp__nonce__picture", {}), ("[image/png image omitted]", False))

    def test_an_unknown_tool_is_an_error(self):
        print("\n  ▶ TestMcpStdio.test_an_unknown_tool_is_an_error")
        text, is_error = tools.run_tool("mcp__nonce__missing", {})
        self.assertTrue(is_error)

    def test_status_names_the_server_and_its_tools(self):
        print("\n  ▶ TestMcpStdio.test_status_names_the_server_and_its_tools")
        text = "\n".join(mcp_servers.status_lines())
        self.assertIn("nonce", text)
        self.assertIn("4 tools", text)

    def test_zz_api_turn_round_trip(self):
        print("\n  ▶ TestMcpStdio.test_zz_api_turn_round_trip")
        import anthropic
        import llm_light.config as config
        from llm_light import llm

        _StubMessagesAPI.requests = []
        server = HTTPServer(("127.0.0.1", 0), _StubMessagesAPI)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            stub = anthropic.Anthropic(api_key="sk-ant-stub",
                                       base_url=f"http://127.0.0.1:{server.server_port}")
            kwargs = dict(model="claude-sonnet-5-5", max_tokens=2000,
                          system=llm._build_system_blocks("SKELETON"),
                          messages=[{"role": "user", "content": [{"type": "text", "text": "nonce?"}]}])
            llm._apply_effort(kwargs, "medium")
            with patch.object(config, "AUTH_MODE", "API_KEY"), \
                 patch.object(_claude_mod, "client", stub), \
                 patch("sys.stdout", io.StringIO()):
                reply, _, _ = _claude_mod._make_streaming_api_call(**kwargs)
        finally:
            server.shutdown()
            server.server_close()

        reqs = _StubMessagesAPI.requests
        self.assertEqual(len(reqs), 2)
        self.assertIn("mcp__nonce__nonce", [t["name"] for t in reqs[0]["tools"]])
        self.assertEqual([t["name"] for t in reqs[0]["tools"]][:5],
                         ["read_file", "fetch_chunk", "grep", "list_files", "shell"])
        result = reqs[1]["messages"][-1]["content"][0]
        self.assertEqual(result["tool_use_id"], "toolu_m1")
        self.assertEqual(result["content"], "Z-" + _NONCE)
        self.assertNotIn("is_error", result)
        self.assertEqual(reply, "done")


class TestMcpStartFailures(unittest.TestCase):
    """A server that cannot start must not hold up or break the session."""

    def test_missing_command_and_hung_server_fail_fast(self):
        print("\n  ▶ TestMcpStartFailures.test_missing_command_and_hung_server_fail_fast")
        from llm_light.mcp_servers import _Manager
        manager = _Manager()
        started = time.monotonic()
        lines = manager.start({
            "missing": {"type": "stdio", "command": "no-such-program-llm-light", "args": [], "env": {}},
            "hung": {"type": "stdio", "command": sys.executable,
                     "args": ["-c", "import time; time.sleep(60)"], "env": {}},
        }, timeout=3)
        elapsed = time.monotonic() - started
        manager.stop()
        self.assertLess(elapsed, 20)
        self.assertEqual(manager.definitions, [])
        text = "\n".join(lines)
        self.assertIn("missing", text)
        self.assertIn("hung", text)
        self.assertIn("3", text)  # names the timeout


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
     "content_block": {"type": "tool_use", "id": "toolu_m1", "name": "mcp__nonce__nonce", "input": {}}},
    {"type": "content_block_delta", "index": 0,
     "delta": {"type": "input_json_delta", "partial_json": json.dumps({"label": "Z-"})}},
    {"type": "content_block_stop", "index": 0},
    {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
     "usage": {"output_tokens": 20}},
    {"type": "message_stop"},
]
_ANSWER_TURN = [
    _start(300),
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "done"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
     "usage": {"output_tokens": 15}},
    {"type": "message_stop"},
]


class _StubMessagesAPI(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append(body)
        data = _sse(_TOOL_TURN if len(type(self).requests) == 1 else _ANSWER_TURN)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    unittest.main()
