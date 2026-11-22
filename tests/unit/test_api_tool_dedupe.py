"""A read-only tool call repeated in the same API-key turn is not run or sent again (#326).

Each round appends every tool result to the turn's messages, and every later round of
the turn sends them all again. A read_file, grep, list_files or fetch_chunk the model
repeats with the same input added the same output, up to 40,000 characters, a second
time. The repeat now gets a one-line note naming the earlier result, unless something
that may change files (shell, an MCP tool) ran in between, or the earlier call failed.
"""

from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

import llm_light  # noqa: F401
from llm_light.providers import claude as _claude_mod

_USAGE = NS(input_tokens=1, output_tokens=1, cache_read_input_tokens=0, cache_creation_input_tokens=0)


def _turn(rounds, fail=()):
    """Run _run_tool_rounds over `rounds` (each a list of (id, name, input)); return
    (the tool calls actually run, {tool_use_id: content sent})."""
    replies = [("", {"message": NS(stop_reason="tool_use", usage=_USAGE,
                                   content=[NS(type="tool_use", id=i, name=n, input=inp)
                                            for i, n, inp in calls])})
               for calls in rounds]
    replies.append(("done", {"message": NS(stop_reason="end_turn", content=[], usage=_USAGE)}))
    replies = iter(replies)
    sent = []
    ran = []

    def stream(client, view=None, **kwargs):
        sent.append(list(kwargs["messages"]))
        return next(replies)

    def run_tool(name, tool_input, root=None):
        ran.append((name, dict(tool_input)))
        key = (name, tuple(sorted(tool_input.items())))
        return f"OUTPUT of {name} {sorted(tool_input.items())}", key in fail

    with patch.object(_claude_mod, "stream_chat_response", side_effect=stream), \
         patch("llm_light.tools.run_tool", side_effect=run_tool):
        _claude_mod._run_tool_rounds({"model": "m", "messages": []}, [], _claude_mod._TurnUsage(),
                                     [], MagicMock())
    results = {}
    for m in sent[-1]:
        if m.get("role") == "user" and isinstance(m.get("content"), list):
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    results[b["tool_use_id"]] = b["content"]
    return ran, results


READ_A = ("read_file", {"path": "a.py"})


def test_a_repeated_read_in_a_later_round_is_not_sent_again():
    ran, results = _turn([[("t1", *READ_A)], [("t2", *READ_A)]])
    assert ran == [READ_A]
    assert results["t1"].startswith("OUTPUT")
    assert "t1" in results["t2"] and "OUTPUT" not in results["t2"]


def test_key_order_in_the_input_does_not_matter():
    ran, _ = _turn([[("t1", "grep", {"pattern": "x", "glob": "*.py"})],
                    [("t2", "grep", {"glob": "*.py", "pattern": "x"})]])
    assert len(ran) == 1


def test_a_different_input_runs():
    ran, _ = _turn([[("t1", *READ_A)], [("t2", "read_file", {"path": "b.py"})]])
    assert len(ran) == 2


def test_a_repeat_in_the_same_round_is_not_sent_twice():
    ran, results = _turn([[("t1", *READ_A), ("t2", "grep", {"pattern": "x"}), ("t3", *READ_A)]])
    assert ran.count(READ_A) == 1
    assert "t1" in results["t3"] and "OUTPUT" not in results["t3"]


def test_shell_in_between_makes_the_read_run_again():
    ran, results = _turn([[("t1", *READ_A)], [("t2", "shell", {"command": "python fix.py"})],
                          [("t3", *READ_A)]])
    assert ran.count(READ_A) == 2
    assert results["t3"].startswith("OUTPUT")


def test_shell_earlier_in_the_same_round_makes_the_read_run_again():
    ran, _ = _turn([[("t1", *READ_A)], [("t2", "shell", {"command": "python fix.py"}), ("t3", *READ_A)]])
    assert ran.count(READ_A) == 2


def test_an_mcp_tool_in_between_makes_the_read_run_again():
    ran, _ = _turn([[("t1", *READ_A)], [("t2", "mcp__fs__write", {"path": "a.py"})], [("t3", *READ_A)]])
    assert ran.count(READ_A) == 2


def test_a_failed_call_is_tried_again():
    key = ("read_file", (("path", "a.py"),))
    ran, _ = _turn([[("t1", *READ_A)], [("t2", *READ_A)]], fail={key})
    assert ran.count(READ_A) == 2


def test_shell_is_never_deduplicated():
    ran, _ = _turn([[("t1", "shell", {"command": "pytest"})], [("t2", "shell", {"command": "pytest"})]])
    assert len(ran) == 2
