"""#209: Esc stops a turn as Ctrl+C does, and keys typed meanwhile wait in the next prompt.

The parser and the listener are tested with a fake key source; the last two tests press
real keys in a real terminal: a pty on POSIX and a console of its own on Windows. Each runs
only on its platform (CI runs Linux, so the Windows one runs on a developer's machine)."""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import llm_light.state as state
from llm_light import esc_key

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture(autouse=True)
def _no_typeahead():
    esc_key.take_typeahead()
    yield
    esc_key.take_typeahead()


# --- reading keys ---------------------------------------------------------------------------

@pytest.mark.parametrize("chunks, events", [
    (["\x1b"], ["esc"]),
    (["ab", "c"], ["a", "b", "c"]),
    (["\x1b[A"], []),                    # up arrow (POSIX): not Esc
    (["\x1bOP"], []),                    # F1 (POSIX)
    (["\x1b[1;5C"], []),                 # ctrl+right (POSIX)
    (["\xe0H", "\x00;"], []),            # up arrow, F1 (Windows getwch)
    (["x\x1b"], ["x", "esc"]),
    (["ab\x7f", "\x08"], ["a", "b", "backspace", "backspace"]),
    (["\r", "\n", "\t"], []),            # never submit or complete on the user's behalf
])
def test_parser_finds_esc_and_text(chunks, events):
    parser = esc_key.Parser()
    found = []
    for chunk in chunks:
        found += parser.feed(chunk, more_pending=False)
    assert found == events


def test_a_split_escape_sequence_is_not_esc():
    # The terminal can deliver "\x1b" and "[A" in two reads; while more input is pending
    # the parser waits rather than calling it Esc.
    parser = esc_key.Parser()
    assert parser.feed("\x1b", more_pending=True) == []
    assert parser.feed("[A", more_pending=False) == []


class _FakeKeys:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.opened = self.closed = False

    def open(self):
        self.opened = True

    def read(self, timeout):
        time.sleep(0.01)
        return (self.chunks.pop(0), False) if self.chunks else ("", False)

    def close(self):
        self.closed = True


def _listen(chunks, on_escape):
    keys = _FakeKeys(chunks)
    with patch.object(esc_key, "_enabled", return_value=True), \
         patch.object(esc_key, "_key_source", return_value=keys):
        with esc_key.listening(on_escape=on_escape) as listener:
            deadline = time.monotonic() + 2
            while keys.chunks and time.monotonic() < deadline:
                time.sleep(0.01)
            time.sleep(0.05)
    return keys, listener


def test_esc_calls_the_interrupt_once_and_the_terminal_is_put_back():
    calls = []
    keys, _ = _listen(["he", "\x1b", "\x1b"], lambda: calls.append(1))
    assert calls == [1]
    assert keys.opened and keys.closed


def test_keys_typed_during_the_turn_wait_for_the_next_prompt():
    _listen(["fix ", "the tesx", "\x7f", "t"], lambda: None)
    assert esc_key.take_typeahead() == "fix the test"
    assert esc_key.take_typeahead() == ""  # taken once


def test_without_a_terminal_nothing_listens():
    with patch.object(esc_key, "_enabled", return_value=False), \
         patch.object(esc_key, "_key_source") as source:
        with esc_key.listening() as listener:
            assert not listener.active
    source.assert_not_called()


def test_a_headless_run_never_listens():
    # -p writes events for a script; it has no keyboard to read.
    with patch.object(sys.stdin, "isatty", return_value=True), \
         patch.object(sys.stdout, "isatty", return_value=True), \
         patch.object(state, "event_sink", object()):
        assert not esc_key._enabled()


def test_the_hint_names_esc_only_where_it_is_read():
    with patch.object(esc_key, "_enabled", return_value=True):
        assert esc_key.stop_key() == "esc"
    with patch.object(esc_key, "_enabled", return_value=False):
        assert esc_key.stop_key() == "ctrl+c"


def test_esc_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("LLM_LIGHT_ESC", "0")
    with patch.object(sys.stdin, "isatty", return_value=True), \
         patch.object(sys.stdout, "isatty", return_value=True):
        assert not esc_key._enabled()


def test_chat_listens_only_while_the_model_answers():
    # Not during the edit approval prompt or a menu, which read the keyboard themselves.
    from llm_light import llm, providers
    seen = []

    class _Recorder:
        active = True

        def __enter__(self):
            seen.append("listen")
            return self

        def __exit__(self, *exc):
            seen.append("stop")
            return False

    def complete(_kwargs):
        seen.append("complete")
        return "plain answer", None, False
    provider = providers.active()
    with patch.object(esc_key, "listening", return_value=_Recorder()), \
         patch.object(provider, "complete", side_effect=complete), \
         patch.object(llm, "retrieve", return_value=("", [])), \
         patch("sys.stdout"):
        llm.chat("what is this?", use_index=False)
    assert seen == ["listen", "complete", "stop"]


# --- real keys in a real terminal -------------------------------------------------------------

def _wait_for(path, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists() and path.read_text(encoding="utf-8"):
            return True
        time.sleep(0.1)
    return False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pty; the Windows console test covers Windows")
def test_esc_in_a_real_pty_interrupts_and_keeps_typeahead(tmp_path):
    import pty
    out, ready = tmp_path / "out.json", tmp_path / "ready"
    pid, fd = pty.fork()
    if pid == 0:  # the child: a turn in a real terminal
        os.execv(sys.executable, [sys.executable, str(_FIXTURES / "esc_child.py"), str(out), str(ready)])
    try:
        assert _wait_for(ready, 60), "the child never started listening"
        time.sleep(0.2)
        os.write(fd, b"hi\x1b")
        assert _wait_for(out, 10), "Esc did not end the turn"
        result = json.loads(out.read_text(encoding="utf-8"))
        assert result == {"interrupted": True, "fast": True, "listening": True, "typeahead": "hi",
                          "echo_restored": True}
    finally:
        try:
            os.kill(pid, 9)
        except OSError:
            pass
        os.waitpid(pid, 0)


@pytest.mark.skipif(sys.platform != "win32", reason="a Windows console; the pty test covers POSIX")
def test_esc_in_a_real_windows_console_interrupts_and_keeps_typeahead(tmp_path):
    out, ready = tmp_path / "out.json", tmp_path / "ready"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
    child = subprocess.Popen([sys.executable, str(_FIXTURES / "esc_child.py"), str(out), str(ready)],
                             creationflags=subprocess.CREATE_NEW_CONSOLE, env=env)
    try:
        assert _wait_for(ready, 60), "the child never started listening"
        time.sleep(0.2)
        typed = subprocess.run([sys.executable, str(_FIXTURES / "console_keys.py"), str(child.pid), "hi\\x1b"],
                               capture_output=True, text=True, timeout=30,
                               creationflags=subprocess.DETACHED_PROCESS)
        assert typed.returncode == 0, typed.stderr
        assert _wait_for(out, 10), "Esc did not end the turn"
        result = json.loads(out.read_text(encoding="utf-8"))
        assert result == {"interrupted": True, "fast": True, "listening": True, "typeahead": "hi"}
    finally:
        child.kill()
        child.wait(10)
