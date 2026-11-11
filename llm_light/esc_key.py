"""Esc stops a turn as Ctrl+C does (#209).

Nothing read the keyboard during a turn: keys waited in the terminal for the next prompt,
so only Ctrl+C could stop one, and a second Ctrl+C at an empty prompt quits. While the
model answers in an interactive session, listening() reads keys on a thread:

- Esc stops the turn by the path Ctrl+C takes: KeyboardInterrupt in the main thread, the
  agent CLI's process tree killed, chat() printing "Interrupted". On POSIX it sends SIGINT
  to this process, which also breaks a blocking read. On Windows it raises the interrupt
  with _thread.interrupt_main() and kills the turn's agent process (state.turn_process),
  since the interrupt waits for a blocking read to return: a pipe read with no output ran
  its full 8 s alone and stopped in 0.5 s with the child killed. A console CTRL_C_EVENT was
  not used, because a process started with Ctrl+C ignored (a flag children inherit) never
  sees one. With an API key there is no child, so it closes the open response stream
  (state.turn_stream) instead, which ends the read: a stream silent for 10 s stopped in
  0.5 s, and the next request on the same client answered (#229).
- Any other text is kept and put in the next prompt (take_typeahead()), as it would have
  waited in the terminal. Enter, Tab and other control keys are dropped, so nothing is
  submitted or completed on the user's behalf; an arrow or function key is not Esc.

It never runs for -p (a script has no keyboard) or without a terminal, and only around the
model call, not the edit approval prompt or a menu, which read the keyboard themselves.
LLM_LIGHT_ESC=0 turns it off.
"""

import os
import signal
import sys
import threading
import time
from contextlib import contextmanager

import llm_light.state as state
from llm_light.config import env_get

_POLL_SECS = 0.05
# After Esc, how long stop() waits for the interrupt to arrive, so it lands inside the
# turn's try and not at the prompt, where it would quit.
_DELIVERY_SECS = 0.5


class Parser:
    """Turns raw input into events: "esc", "backspace", or one printable character."""

    def __init__(self):
        self.seq = ""          # an escape sequence being read
        self.skip_next = False  # after a Windows \x00 or \xe0 prefix

    def feed(self, chunk: str, more_pending: bool) -> list:
        events = []
        for ch in chunk:
            if self.skip_next:
                self.skip_next = False
            elif self.seq:
                self.seq += ch
                if self._sequence_done():
                    self.seq = ""
            elif ch == "\x1b":
                self.seq = ch
            elif ch in ("\x00", "\xe0"):
                self.skip_next = True
            elif ch in ("\x7f", "\x08"):
                events.append("backspace")
            elif ch.isprintable():
                events.append(ch)
        if self.seq and not more_pending:
            if self.seq == "\x1b":
                events.append("esc")
            self.seq = ""  # an unfinished sequence is dropped, never read as Esc
        return events

    def _sequence_done(self) -> bool:
        body = self.seq[1:]
        if body[0] == "[":  # CSI: parameters, then a final byte in @..~
            return len(body) > 1 and "@" <= body[-1] <= "~"
        if body[0] == "O":  # SS3: one more character
            return len(body) == 2
        return True  # alt+key: two characters


class _PosixKeys:
    def open(self):
        import termios
        import tty
        self.fd = sys.stdin.fileno()
        self.saved = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd, termios.TCSANOW)  # no echo, no line editing; ctrl+c still signals

    def read(self, timeout):
        import select
        if not select.select([self.fd], [], [], timeout)[0]:
            return "", False
        data = os.read(self.fd, 1024).decode("utf-8", errors="replace")
        # The rest of an escape sequence can arrive in a second read.
        more = bool(select.select([self.fd], [], [], 0.03)[0])
        return data, more

    def close(self):
        import termios
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)


class _WindowsKeys:
    def open(self):
        pass

    def read(self, timeout):
        import msvcrt
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if msvcrt.kbhit():
                chars = []
                while msvcrt.kbhit():
                    chars.append(msvcrt.getwch())
                return "".join(chars), False
            time.sleep(0.01)
        return "", False

    def close(self):
        pass


def _key_source():
    return _WindowsKeys() if os.name == "nt" else _PosixKeys()


def _enabled() -> bool:
    if env_get("ESC") == "0" or state.event_sink is not None:
        return False
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _interrupt() -> None:
    """Do what Ctrl+C does; see the module docstring for why Windows differs."""
    if os.name != "nt":
        os.kill(os.getpid(), signal.SIGINT)  # the CLI child has its own session either way
        return
    _stop_turn_windows()


def _stop_turn_windows() -> None:
    import _thread
    from llm_light.providers.runner import kill_process_tree
    _thread.interrupt_main()
    proc = state.turn_process
    if proc is not None:
        try:
            kill_process_tree(proc)
        except Exception:  # noqa: BLE001  already gone: the read ends anyway
            pass
    # An API-key turn has no process: end its stream's blocking read, which the interrupt
    # waits for (#229). The client opens a new connection for the next request.
    stream = state.turn_stream
    if stream is not None:
        _abort_stream(stream)


def _abort_stream(stream) -> None:
    """Shut the stream's socket down, then close the stream. Closing alone ends a blocked
    read on Windows but not on Linux, where only shutdown() wakes a recv() that another
    thread is blocked in."""
    import socket
    raw = getattr(stream, "_raw_stream", None)
    response = getattr(stream, "response", None) or getattr(raw, "response", None)
    try:
        network = response.extensions.get("network_stream")
        sock = network.get_extra_info("socket") if network is not None else None
    except Exception:  # noqa: BLE001  another transport: closing is all there is
        sock = None
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # already closed by the peer
    try:
        stream.close()
    except Exception:  # noqa: BLE001  closing a stream mid-read may raise; it is closed
        pass


@contextmanager
def running(proc):
    """Mark `proc` as the running turn's agent process, which Esc stops on Windows."""
    state.turn_process = proc
    try:
        yield proc
    finally:
        state.turn_process = None


class _Listener:
    def __init__(self, on_escape):
        self.on_escape = on_escape
        self.active = False
        self.fired = False
        self.typed = ""
        self._stop = threading.Event()
        self._thread = None
        self._source = None

    def start(self, source):
        self._source = source
        source.open()
        self.active = True
        self._thread = threading.Thread(target=self._run, name="llm_light-esc", daemon=True)
        self._thread.start()

    def _run(self):
        parser = Parser()
        while not self._stop.is_set():
            try:
                chunk, more = self._source.read(_POLL_SECS)
            except (OSError, ValueError):
                return  # the terminal went away; Ctrl+C still works
            for event in parser.feed(chunk, more):
                if event == "esc":
                    if not self.fired and not self._stop.is_set():
                        self.fired = True
                        self.on_escape()
                elif event == "backspace":
                    self.typed = self.typed[:-1]
                else:
                    self.typed += event

    def stop(self):
        if not self.active:
            return
        self._stop.set()
        self._thread.join(timeout=2)
        try:
            self._source.close()
        finally:
            self.active = False
            if self.typed:
                with state.lock:
                    state.typeahead += self.typed
        if self.fired:
            time.sleep(_DELIVERY_SECS)  # the interrupt, if not here yet, is raised in this sleep


def _start(on_escape):
    listener = _Listener(on_escape)
    if _enabled():
        try:
            listener.start(_key_source())
        except (OSError, ValueError, ImportError):
            listener.active = False  # no usable terminal: Ctrl+C still works
    return listener


@contextmanager
def listening(on_escape=None):
    """Listen for Esc for the length of the block; see the module docstring."""
    listener = _start(on_escape or _interrupt)
    state.esc_listener = listener
    try:
        yield listener
    finally:
        current = state.esc_listener
        state.esc_listener = None
        if current is not None and current is not listener:
            current.stop()  # the one paused() started
        listener.stop()


@contextmanager
def paused():
    """Stop listening for the length of the block, for a question asked in the middle of a
    turn (ui.ask_mid_turn): the listener would read the keys meant for the menu. A new
    listener with the same action starts afterwards. Without one running, nothing."""
    listener = state.esc_listener
    if listener is None or not listener.active:
        yield
        return
    listener.stop()
    state.esc_listener = None
    try:
        yield
    finally:
        state.esc_listener = _start(listener.on_escape)


def stop_key() -> str:
    """The key to name in "... to interrupt": esc where it is read, else ctrl+c."""
    return "esc" if _enabled() else "ctrl+c"


def take_typeahead() -> str:
    """What was typed during the last turn, once; the next prompt starts with it."""
    with state.lock:
        text, state.typeahead = state.typeahead, ""
    return text
