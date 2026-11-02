"""
Tests for llm_light.callgraph and the fetch_chunk tool, and that retrieval no
longer expands with the call graph (#86).
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy-test-key")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import llm_light.state as state
from llm_light import callgraph
from llm_light import retrieval
from llm_light.retrieval import _finish
from llm_light.tools import run_tool

_SEP = "\n    // ...\n"


def _chunk(path, name, body):
    return {"id": f"{path}::{name}", "text": f"// {path}\nimport os{_SEP}{body}\n"}


FIXTURE = [
    _chunk("app/api.py", "handle", "def handle(req):\n    user = load_user(req)\n    return render(user)"),
    _chunk("app/api.py", "load_user", "def load_user(req):\n    return validate(req.id)"),
    _chunk("app/api.py", "render", "def render(user):\n    return str(user)"),
    _chunk("app/auth.py", "validate", "def validate(uid):\n    return uid > 0"),
    _chunk("app/cli.py", "main", "def main():\n    validate(1)\n    get()"),
    # 'get' is defined in four files: too generic to follow
    _chunk("a.py", "get", "def get():\n    pass"),
    _chunk("b.py", "get", "def get():\n    pass"),
    _chunk("c.py", "get", "def get():\n    pass"),
    _chunk("d.py", "get", "def get():\n    pass"),
]


class _StoreCase(unittest.TestCase):
    def setUp(self):
        with state.lock:
            self._orig = dict(state.chunk_store)
            state.chunk_store.clear()
            for c in FIXTURE:
                state.chunk_store[c["id"]] = {"text": c["text"], "emb": np.zeros(2)}
        callgraph._names_cache.clear()

    def tearDown(self):
        with state.lock:
            state.chunk_store.clear()
            state.chunk_store.update(self._orig)
        callgraph._names_cache.clear()


class TestCallGraph(_StoreCase):

    def test_called_names_skips_keywords_and_header(self):
        print("\n  ▶ TestCallGraph.test_called_names_skips_keywords_and_header")
        names = callgraph.called_names(FIXTURE[0]["text"])
        self.assertEqual(names, {"handle", "load_user", "render"})  # def line counts; `if` etc. do not
        self.assertNotIn("import", names)

    def test_callees_and_callers(self):
        print("\n  ▶ TestCallGraph.test_callees_and_callers")
        callees, callers = callgraph.related_chunks("app/api.py::handle")
        self.assertEqual(callees, ["app/api.py::load_user", "app/api.py::render"])
        self.assertEqual(callers, [])
        callees, callers = callgraph.related_chunks("app/auth.py::validate")
        self.assertEqual(callees, [])
        self.assertEqual(callers, ["app/api.py::load_user", "app/cli.py::main"])

    def test_generic_names_are_not_followed(self):
        print("\n  ▶ TestCallGraph.test_generic_names_are_not_followed")
        callees, _ = callgraph.related_chunks("app/cli.py::main")
        self.assertEqual(callees, ["app/auth.py::validate"])

    def test_unknown_or_whole_file_id(self):
        print("\n  ▶ TestCallGraph.test_unknown_or_whole_file_id")
        self.assertEqual(callgraph.related_chunks("nope::x"), ([], []))
        self.assertEqual(callgraph.related_chunks("app/api.py"), ([], []))

    def test_edit_invalidates_cache(self):
        print("\n  ▶ TestCallGraph.test_edit_invalidates_cache")
        self.assertEqual(callgraph.related_chunks("app/api.py::render")[0], [])
        with state.lock:
            state.chunk_store["app/api.py::render"]["text"] = \
                _chunk("app/api.py", "render", "def render(u):\n    return load_user(u)")["text"]
        self.assertEqual(callgraph.related_chunks("app/api.py::render")[0], ["app/api.py::load_user"])

    def test_interleaved_alternates_callee_first(self):
        print("\n  ▶ TestCallGraph.test_interleaved_alternates_callee_first")
        with state.lock:
            state.chunk_store["app/api.py::load_user"]["text"] = \
                _chunk("app/api.py", "load_user", "def load_user(r):\n    return render(r)\n    validate(r)")["text"]
        out = callgraph.interleaved("app/api.py::load_user")
        self.assertEqual(out[0], "app/api.py::render")           # callee
        self.assertIn("app/api.py::handle", out)                  # caller
        self.assertEqual(len(out), len(set(out)))


class TestFetchChunk(_StoreCase):

    def fetch(self, ids):
        return run_tool("fetch_chunk", {"ids": ids})

    def test_exact_id_returns_body_without_preamble(self):
        print("\n  ▶ TestFetchChunk.test_exact_id_returns_body_without_preamble")
        out, is_error = self.fetch(["app/auth.py::validate"])
        self.assertFalse(is_error)
        self.assertIn("def validate(uid)", out)
        self.assertNotIn("import os", out)
        self.assertIn("Called by: app/api.py::load_user, app/cli.py::main", out)

    def test_bare_symbol_and_backslash_path(self):
        print("\n  ▶ TestFetchChunk.test_bare_symbol_and_backslash_path")
        self.assertIn("def render(user)", self.fetch(["render"])[0])
        with state.lock:
            cid = "app\\auth.py::validate"
            state.chunk_store[cid] = state.chunk_store.pop("app/auth.py::validate")
        self.assertIn("def validate(uid)", self.fetch(["app/auth.py::validate"])[0])

    def test_ambiguous_and_missing(self):
        print("\n  ▶ TestFetchChunk.test_ambiguous_and_missing")
        out, is_error = self.fetch(["get", "zzz"])
        self.assertFalse(is_error)
        self.assertIn("Ambiguous", out)
        self.assertIn("a.py::get", out)
        self.assertIn("No such chunk", out)

    def test_bad_input_is_an_error(self):
        print("\n  ▶ TestFetchChunk.test_bad_input_is_an_error")
        self.assertTrue(run_tool("fetch_chunk", {})[1])
        self.assertTrue(run_tool("fetch_chunk", {"ids": []})[1])
        self.assertTrue(run_tool("fetch_chunk", {"ids": [3]})[1])

    def test_empty_index_is_an_error(self):
        print("\n  ▶ TestFetchChunk.test_empty_index_is_an_error")
        with state.lock:
            state.chunk_store.clear()
        out, is_error = self.fetch(["x"])
        self.assertTrue(is_error)
        self.assertIn("read_file", out)

    def test_caps_number_of_ids(self):
        print("\n  ▶ TestFetchChunk.test_caps_number_of_ids")
        out, _ = self.fetch(["render"] * 9)
        self.assertEqual(out.count("## "), 5)


class TestNoRetrievalExpansion(_StoreCase):
    """Call-graph expansion was removed (#86): in a live answer-quality run it
    won 2 and lost 2 of 8 how-is-X-used questions for 92% more context. The
    call graph stays for fetch_chunk, which lists callers and callees on request."""

    def test_finish_sends_only_the_selected_chunks(self):
        top = [("app/api.py::handle", 0.9)]
        for effort in ("medium", "high", "max"):
            ctx, pairs = _finish(top, effort)
            self.assertEqual(pairs, top)
            self.assertNotIn("def load_user(req)", ctx)  # a callee of handle

    def test_the_old_switch_does_nothing(self):
        with patch.dict(os.environ, {"LLM_LIGHT_CALLGRAPH": "1"}):
            _, pairs = _finish([("app/api.py::handle", 0.9)], "medium")
        self.assertEqual(len(pairs), 1)

    def test_the_expansion_path_is_gone(self):
        from llm_light import config
        self.assertFalse(hasattr(retrieval, "_related_pairs"))
        self.assertFalse(hasattr(config, "CALL_GRAPH"))
        self.assertFalse(hasattr(config, "CALL_GRAPH_MAX"))


if __name__ == "__main__":
    unittest.main()
