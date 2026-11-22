"""What an attachment leaves in the conversation history (#322).

An @file, piped text, !command output or a /review diff goes with the question it
was attached to in full. The history kept all of it too, so a 50,000-character file
was sent again on every later turn until compression dropped it (up to six turns),
and saved to disk, as it was when attached even after an edit changed it. The
history now keeps a short note for each attached file and the start and end of any
other long attachment; short attachments stay as they are.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

import llm_light.state as state
from llm_light import config, llm


def _usage():
    usage = MagicMock()
    usage.input_tokens = 10
    usage.output_tokens = 5
    usage.cache_creation_input_tokens = 0
    usage.cache_read_input_tokens = 0
    return usage


@pytest.fixture(autouse=True)
def _clean_history():
    saved = (list(state.conversation_history), state.turn_count)
    state.conversation_history.clear()
    yield
    state.conversation_history[:] = saved[0]
    state.turn_count = saved[1]


def _turns(*turns):
    """Run real chat() turns, each (question, context); return the request each sent."""
    sent = []

    def fake_call(**kwargs):
        sent.append(json.dumps(kwargs, default=str))
        return "an answer", _usage(), False

    with patch("llm_light.llm.route_query", return_value=(config.MODEL_SONNET, "medium", 4_096)), \
         patch("llm_light.providers.claude._make_streaming_api_call", side_effect=fake_call), \
         patch("llm_light.llm.retrieve", return_value=("", [])), \
         patch("llm_light.llm.parse_edit_blocks", return_value=[]), \
         patch("llm_light.llm._maybe_compress_history"), \
         patch("llm_light.llm._print_reply"), \
         patch("llm_light.llm.print_stats"), \
         patch("llm_light.config.LLMLINGUA_ENABLED", False):
        for question, context in turns:
            llm.chat(question, context=context)
    return sent


BODY = "".join(f"def helper_{i}():\n    return {i}  # MARKER_{i}\n" for i in range(1_500))
FILE_CONTEXT = f'Files the user attached:\n\n<file path="src/big.py">\n{BODY}\n</file>'


def test_the_turn_it_is_attached_to_gets_all_of_it():
    first, = _turns(("explain @src/big.py", FILE_CONTEXT))
    assert "MARKER_0" in first and "MARKER_1499" in first


def test_later_turns_do_not_send_an_attached_file_again():
    _, second = _turns(("explain @src/big.py", FILE_CONTEXT), ("and the last helper?", ""))
    assert "MARKER_700" not in second
    assert "src/big.py" in second            # the note still names it, so it can be read again


def test_the_history_keeps_a_note_for_the_file():
    _turns(("explain @src/big.py", FILE_CONTEXT))
    kept = state.conversation_history[0]["content"]
    assert len(kept) < 2_000
    assert "src/big.py" in kept and "explain @src/big.py" in kept
    assert "3,000 lines" in kept              # its size


def test_long_command_output_keeps_its_start_and_its_end():
    # The error a build prints last is what a follow-up asks about.
    log = "START OF LOG\n" + "compiling module\n" * 5_000 + "error: THE REAL FAILURE\n"
    context = f"I ran `make` in the project root. Its output:\n```\n{log}```"
    _turns(("why did make fail?", context))
    kept = state.conversation_history[0]["content"]
    assert len(kept) < 2_000
    assert "START OF LOG" in kept and "THE REAL FAILURE" in kept
    assert "left out of the history" in kept


def test_a_short_attachment_stays_as_it_is():
    context = "I ran `git status` in the project root. Its output:\n```\nnothing to commit\n```"
    _turns(("is the tree clean?", context))
    assert state.conversation_history[0]["content"] == "is the tree clean?\n\n" + context
