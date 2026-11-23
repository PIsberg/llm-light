"""Retrieval does not send what the request already holds (#324).

* A file attached with @path goes with the question in full; its chunks took
  retrieval slots (often the first, since an attached file is usually the one being
  edited and gets the working-tree boost) and sent the same code twice.
* A greeting or meta request ("hi", "help", "compact") routes to Haiku at low
  effort with no scoring, yet retrieval still ran and attached whatever "help"
  matched, such as _print_help.
"""

import json
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

import llm_light as cl
import llm_light.state as state
from llm_light import config, llm


@pytest.fixture
def store():
    saved = (dict(cl.chunk_store), cl.TOP_K, cl.EMBED_MODEL, cl.embedder, cl.MIN_SCORE,
             cl.RELATIVE_SCORE_FLOOR)
    cl.chunk_store.clear()
    cl.chunk_store.update({
        "src/A.py::func_a": {"text": "def func_a(): pass", "emb": np.array([1.0, 0.0, 0.0]),
                             "emb_fast": np.array([1.0, 0.0, 0.0])},
        "src/A.py::func_a2": {"text": "def func_a2(): pass", "emb": np.array([0.9, 0.1, 0.0]),
                              "emb_fast": np.array([0.9, 0.1, 0.0])},
        "src\\B.py::func_b": {"text": "def func_b(): pass", "emb": np.array([0.7, 0.7, 0.0]),
                               "emb_fast": np.array([0.7, 0.7, 0.0])},
        "src/C.py::func_c": {"text": "def func_c(): pass", "emb": np.array([0.6, 0.0, 0.8]),
                             "emb_fast": np.array([0.6, 0.0, 0.8])},
    })
    cl.TOP_K = 5
    cl.EMBED_MODEL = "all-MiniLM-L6-v2"
    cl.MIN_SCORE = 0.0
    cl.RELATIVE_SCORE_FLOOR = 0.0
    embedder = MagicMock()
    embedder.encode.return_value = np.array([1.0, 0.0, 0.0])
    cl.embedder = embedder
    state.embedder_ready.set()
    yield embedder
    cl.chunk_store.clear()
    cl.chunk_store.update(saved[0])
    (_, cl.TOP_K, cl.EMBED_MODEL, cl.embedder, cl.MIN_SCORE, cl.RELATIVE_SCORE_FLOOR) = saved
    state.embedder_ready.clear()


def _files(hits):
    return {cid.split("::")[0].replace("\\", "/") for cid, _ in hits}


def test_without_an_attachment_the_attached_file_would_lead(store):
    _, hits = cl.retrieve("func a", token_budget=6_000)
    assert "src/A.py" in _files(hits)


def test_an_attached_files_chunks_are_not_retrieved(store):
    _, everything = cl.retrieve("func a", token_budget=6_000)
    _, hits = cl.retrieve("func a", token_budget=6_000, exclude_files={"src/A.py"})
    assert "src/A.py" not in _files(hits)
    # The copies are dropped; nothing new is pulled in to take their place.
    assert hits == [h for h in everything if not h[0].startswith("src/A.py")]


def test_the_match_ignores_the_path_separator(store):
    # On Windows the index keys files with a backslash; @ paths use a forward slash.
    _, hits = cl.retrieve("func a", token_budget=6_000, exclude_files={"src/B.py"})
    assert "src/B.py" not in _files(hits)


def test_excluding_every_file_retrieves_nothing(store):
    assert cl.retrieve("func a", exclude_files={"src/A.py", "src/B.py", "src/C.py"}) == ("", [])


def test_the_hybrid_path_excludes_them_too(store, monkeypatch):
    fast = MagicMock()
    fast.encode.return_value = np.array([1.0, 0.0, 0.0])
    monkeypatch.setattr(config, "HYBRID_RETRIEVAL", True)
    monkeypatch.setattr(state, "hybrid_index", True)
    monkeypatch.setattr(state, "fast_embedder", fast)
    state.fast_ready.set()
    for entry in cl.chunk_store.values():   # static vectors only: no dense model needed
        entry.pop("emb", None)
    _, hits = cl.retrieve("func a", token_budget=6_000)
    assert "src/A.py" in _files(hits)
    _, everything = cl.retrieve("func a", token_budget=6_000)
    _, hits = cl.retrieve("func a", token_budget=6_000, exclude_files={"src/A.py"})
    assert "src/A.py" not in _files(hits)
    assert hits == [h for h in everything if not h[0].startswith("src/A.py")]


# --- what chat() and one-shot turns ask retrieval for -----------------------------------

def _usage():
    u = MagicMock()
    u.input_tokens, u.output_tokens = 10, 5
    u.cache_creation_input_tokens = u.cache_read_input_tokens = 0
    return u


@pytest.fixture
def turn():
    saved = (list(state.conversation_history), state.turn_count)
    state.conversation_history.clear()
    retrieve = MagicMock(return_value=("", []))
    with patch("llm_light.providers.claude._make_streaming_api_call",
               side_effect=lambda **kw: ("answer", _usage(), False)), \
         patch.object(llm, "retrieve", retrieve), \
         patch.object(llm, "parse_edit_blocks", return_value=[]), \
         patch.object(llm, "_maybe_compress_history"), \
         patch.object(llm, "_print_reply"), patch.object(llm, "print_stats"), \
         patch("llm_light.config.LLMLINGUA_ENABLED", False):
        yield retrieve
    state.conversation_history[:] = saved[0]
    state.turn_count = saved[1]


ATTACHED = 'Files the user attached:\n\n<file path="src/A.py">\ndef func_a(): pass\n</file>'


def test_chat_excludes_the_files_it_attached(turn):
    llm.chat("fix @src/A.py", context=ATTACHED)
    assert turn.call_args.kwargs["exclude_files"] == {"src/A.py"}


def test_chat_without_attachments_excludes_nothing(turn):
    llm.chat("where is the retry loop?")
    assert not turn.call_args.kwargs.get("exclude_files")


@pytest.mark.parametrize("greeting", ["hi", "Hello!", "help", "compact", "who are you?"])
def test_a_greeting_retrieves_nothing(turn, greeting):
    llm.chat(greeting)
    turn.assert_not_called()


def test_a_question_that_starts_with_help_still_retrieves(turn):
    llm.chat("help me find the retry loop")
    turn.assert_called_once()


def test_a_one_shot_turn_excludes_attached_files_and_skips_greetings(turn):
    # The turn itself: one_shot() first indexes the working directory.
    llm._one_shot_turn("fix @src/A.py", False, True, context=ATTACHED)
    assert turn.call_args.kwargs["exclude_files"] == {"src/A.py"}
    turn.reset_mock()
    llm._one_shot_turn("hi", False, True)
    turn.assert_not_called()
