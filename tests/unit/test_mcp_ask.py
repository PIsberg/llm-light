"""#234: in default mode with an API key, an MCP call runs only when the user says yes.

Claude Code asks before an MCP tool call unless a rule allows it; llm_light runs the API-key
tool loop itself, so it asks there too: the tool and its arguments, then yes, yes for this
tool for the rest of the session, or no. The question pauses the streamed reply's spinner
and the Esc listener, which would otherwise draw over the menu and eat its keys. Without a
terminal (a -p run, piped input) nothing can be asked, and the mcp.json allow list stays the
only gate, as before.
"""

from contextlib import contextmanager
from unittest.mock import patch

import pytest

import llm_light.state as state
from llm_light import esc_key, mcp_servers, permissions, tools, ui


@pytest.fixture
def mode():
    allowed = state.bypass_allowed

    def set_mode(name):
        state.bypass_allowed = True
        permissions.set_mode(name)
    yield set_mode
    permissions.set_mode(permissions.DEFAULT)
    state.bypass_allowed = allowed
    state.mcp_always_allowed.clear()


@pytest.fixture
def server():
    with patch.object(mcp_servers, "call", return_value=("N0NCE", False)) as call:
        yield call


@pytest.fixture
def answers(monkeypatch):
    """answers(*values): the user's picks, in order; .asked holds each question."""
    asked = []

    def install(*values):
        it = iter(values)
        monkeypatch.setattr(ui, "can_ask", lambda: True)
        monkeypatch.setattr(ui, "choose", lambda q, options, default=0: asked.append((q, options)) or next(it))
    install.asked = asked
    return install


def _call(args=None):
    return tools.run_tool("mcp__github__create_issue", args or {"title": "bug"})


def test_yes_runs_the_call_after_showing_the_tool_and_its_arguments(mode, server, answers):
    mode(permissions.DEFAULT)
    answers("yes")
    assert _call({"title": "bug in parser"}) == ("N0NCE", False)
    (question, options), = answers.asked
    assert "mcp__github__create_issue" in question and "bug in parser" in question
    assert [v for v, _ in options] == ["yes", "always", "no"]


@pytest.mark.parametrize("answer", ["no", None])  # None: Esc or Ctrl+C at the menu
def test_no_refuses_the_call_and_tells_the_model(mode, server, answers, answer):
    mode(permissions.DEFAULT)
    answers(answer)
    text, is_error = _call()
    assert is_error and "declined" in text
    server.assert_not_called()


def test_always_stops_asking_for_that_tool_only(mode, server, answers):
    mode(permissions.DEFAULT)
    answers("always", "yes")
    _call()
    _call()
    assert len(answers.asked) == 1
    tools.run_tool("mcp__github__close_issue", {})
    assert len(answers.asked) == 2  # another tool still asks


@pytest.mark.parametrize("name", [permissions.ACCEPT_EDITS, permissions.BYPASS])
def test_other_modes_run_unasked_as_before(mode, server, answers, name):
    mode(name)
    answers()
    assert _call() == ("N0NCE", False)
    assert answers.asked == []


def test_without_a_terminal_nothing_is_asked_and_the_call_runs(mode, server, monkeypatch):
    # A -p run cannot answer; the mcp.json allow list is its gate, as before #234.
    mode(permissions.DEFAULT)
    monkeypatch.setattr(ui, "can_ask", lambda: False)
    monkeypatch.setattr(ui, "choose", lambda *a, **k: pytest.fail("asked without a terminal"))
    assert _call() == ("N0NCE", False)


# --- the question pauses the turn's spinner and Esc listener -------------------------------

def test_a_question_mid_turn_pauses_the_view_and_the_esc_listener(monkeypatch):
    events = []

    class View:
        @contextmanager
        def paused(self):
            events.append("view paused")
            yield
            events.append("view resumed")

    @contextmanager
    def esc_paused():
        events.append("esc paused")
        yield
        events.append("esc resumed")
    monkeypatch.setattr(state, "turn_view", View())
    monkeypatch.setattr(esc_key, "paused", esc_paused)
    monkeypatch.setattr(ui, "choose", lambda *a, **k: events.append("asked") or "yes")
    assert ui.ask_mid_turn("Run it?", [("yes", "Yes")]) == "yes"
    assert events == ["view paused", "esc paused", "asked", "esc resumed", "view resumed"]


def test_esc_paused_stops_the_listener_and_starts_a_new_one(monkeypatch):
    class Source:
        def __init__(self):
            self.opened = 0

        def open(self):
            self.opened += 1

        def read(self, timeout):
            import time
            time.sleep(timeout)
            return "", False

        def close(self):
            pass
    source = Source()
    monkeypatch.setattr(esc_key, "_enabled", lambda: True)
    monkeypatch.setattr(esc_key, "_key_source", lambda: source)
    with esc_key.listening(on_escape=lambda: None) as listener:
        with esc_key.paused():
            assert listener.active is False
            assert state.esc_listener is None
        assert state.esc_listener is not None and state.esc_listener.active
        assert source.opened == 2
    assert state.esc_listener is None


def test_esc_paused_without_a_listener_does_nothing():
    with esc_key.paused():
        pass


def test_response_view_pauses_its_heartbeat(monkeypatch):
    calls = []
    view = ui.ResponseView("Thinking", animate=True)
    view._console = None
    monkeypatch.setattr(view, "_stop_heartbeat", lambda: calls.append("stop"))
    monkeypatch.setattr(view, "_start_heartbeat", lambda: calls.append("start"))
    with view.paused():
        calls.append("question")
    assert calls == ["stop", "question", "start"]
