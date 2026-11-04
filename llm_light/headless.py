"""One turn for a script: `-p --output-format json|stream-json`, and `-c -p`.

A script reads stdout, so during the turn stdout is pointed at stderr, where everything
llm_light prints as it works (retrieval, stats, a diff) stays visible. Only the answer
reaches the real stdout, written from the events ResponseView sends to state.event_sink.

    text         the reply, as it arrives
    json         one object when the turn ends
    stream-json  one object per line: system/init, text, tool, then result

The result's fields follow `claude -p --output-format json`, so a script written for one
reads the other. A failed turn sets `is_error` and the process exits non-zero.
"""

import contextlib
import json
import re
import sys
import time
from pathlib import Path

import llm_light.state as state
from llm_light import commands, providers
from llm_light.providers.base import USAGE, USD_BUDGET

FORMATS = ("text", "json", "stream-json")

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _has_usage() -> bool:
    return providers.active().has(USAGE)


def _priced() -> bool:
    return _has_usage() and providers.active().has(USD_BUDGET)


class _Output:
    def __init__(self, fmt: str, stream):
        self.fmt = fmt
        self.stream = stream
        self.wrote_text = False

    def _line(self, event: dict) -> None:
        self.stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        self.stream.flush()

    def event(self, event: dict) -> None:
        """Called from ResponseView, as text and tool calls arrive."""
        if event["type"] == "text":
            self.wrote_text = True
            if self.fmt == "text":
                self.stream.write(event["text"])
                self.stream.flush()
            elif self.fmt == "stream-json":
                self._line(event)
        elif self.fmt == "stream-json":
            self._line(event)
        else:
            print(f"  {event['label']}", file=sys.stderr, flush=True)

    def start(self) -> None:
        if self.fmt == "stream-json":
            self._line({"type": "system", "subtype": "init", "cwd": str(Path.cwd()),
                        "provider": providers.active().name, "version": commands.version()})

    def finish(self, subtype: str, reply: str | None, error: str | None, seconds: float) -> None:
        if reply and not self.wrote_text:  # a provider that hands over the reply whole
            self.event({"type": "text", "text": reply})
        if self.fmt == "text":
            if reply:
                self.stream.write("\n")
                self.stream.flush()
            if error:
                print(error, file=sys.stderr, flush=True)
            return
        with state.lock:
            tokens = dict(state.session_tokens)
            cost = state.session_cost
            model = state.pinned_model or state.session_model
        self._line({
            "type": "result",
            "subtype": subtype,
            "is_error": subtype != "success",
            "result": reply,
            "error": error,
            "model": model,
            "duration_ms": round(seconds * 1000),
            "num_turns": state.turn_count or (1 if reply else 0),
            "usage": {"input_tokens": tokens.get("input", 0),
                      "output_tokens": tokens.get("output", 0),
                      "cache_read_input_tokens": tokens.get("cache_read", 0),
                      "cache_creation_input_tokens": tokens.get("cache_write", 0)} if _has_usage() else None,
            "total_cost_usd": cost if _priced() else None,
        })


def _error_text(code) -> str:
    if isinstance(code, str):
        return _ANSI.sub("", code)
    return f"exited with code {code}"


def run(fmt: str, turn) -> int:
    """Run `turn` with stdout kept for the answer; returns the process's exit code.
    `turn` reports failure by returning False or raising SystemExit."""
    output = _Output(fmt, sys.stdout)
    output.start()
    replies_before = len(state.replies)
    subtype, error, code = "success", None, 0
    started = time.monotonic()
    state.event_sink = output.event
    try:
        with contextlib.redirect_stdout(sys.stderr):
            if turn() is False:
                subtype, error, code = "error", "the turn failed; its error is on stderr", 1
    except SystemExit as stopped:
        if stopped.code not in (0, None):
            subtype, error, code = "error", _error_text(stopped.code), 1
    except KeyboardInterrupt:
        subtype, error, code = "interrupted", "interrupted", 130
    finally:
        state.event_sink = None
    reply = state.replies[-1] if subtype == "success" and len(state.replies) > replies_before else None
    output.finish(subtype, reply, error, time.monotonic() - started)
    return code
