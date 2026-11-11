"""#229: on Windows, Esc stops an API-key turn at once, not at the next streamed event.

_thread.interrupt_main() waits for the main thread's blocking socket read to return, and an
API-key turn has no agent process to kill, so a model still thinking kept the turn going
until its next event. Esc now also closes the open stream (state.turn_stream) from the
listener thread, which ends the read. A local server stands in for the Messages endpoint:
it sends message_start and then nothing for 10 s; a second request gets a normal answer, to
show the client still works after its stream was closed from another thread.
"""

import io
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy-test-key")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import llm_light.state as state  # noqa: E402
from llm_light import esc_key  # noqa: E402
from llm_light.providers import claude as _claude_mod  # noqa: E402

_SILENCE_SECS = 10


def _event(e):
    return f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode()


_START = {"type": "message_start", "message": {
    "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
    "content": [], "stop_reason": None, "stop_sequence": None,
    "usage": {"input_tokens": 10, "output_tokens": 1,
              "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}}
_ANSWER = [
    _START,
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "still works"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
     "usage": {"output_tokens": 3}},
    {"type": "message_stop"},
]


class _Stub(BaseHTTPRequestHandler):
    calls = 0

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        type(self).calls += 1
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if type(self).calls == 1:  # a model that is still thinking
            self.wfile.write(_event(_START))
            self.wfile.flush()
            time.sleep(_SILENCE_SECS)
            return
        for e in _ANSWER:
            self.wfile.write(_event(e))
        self.wfile.flush()


@pytest.fixture
def stub(monkeypatch, tmp_path):
    import anthropic
    import llm_light.config as config
    monkeypatch.chdir(tmp_path)
    _Stub.calls = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = anthropic.Anthropic(api_key="sk-ant-stub", max_retries=0,
                                 base_url=f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setattr(config, "AUTH_MODE", "API_KEY")
    monkeypatch.setattr(_claude_mod, "client", client)
    yield
    server.shutdown()
    server.server_close()


def _turn():
    kwargs = dict(model="claude-sonnet-5-5", max_tokens=200,
                  messages=[{"role": "user", "content": "q"}])
    with patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
        return _claude_mod._make_streaming_api_call(**kwargs)


def test_esc_ends_a_silent_stream_at_once_and_the_client_still_works(stub):
    def press_esc():
        time.sleep(0.5)
        esc_key._stop_turn_windows()
    threading.Thread(target=press_esc, daemon=True).start()
    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        _turn()
    took = time.monotonic() - started
    assert took < 3, f"the turn ran {took:.1f}s after Esc; the stream was not closed"
    assert state.turn_stream is None
    reply, _usage, _ = _turn()
    assert reply == "still works"


def test_the_stream_is_registered_only_while_it_is_open(stub):
    from llm_light import streaming
    seen = []
    original = streaming.StreamingResponseHandler.process_stream

    def spy(self, stream, view=None):
        seen.append(state.turn_stream is stream)
        return original(self, stream, view=view)
    _Stub.calls = 1  # skip the silent response
    with patch.object(streaming.StreamingResponseHandler, "process_stream", spy):
        _turn()
    assert seen == [True] and state.turn_stream is None
