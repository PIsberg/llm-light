"""A retrieved chunk's file preamble is capped and comment-stripped (#323).

Every chunk carries the lines before its file's first symbol: imports, a module
docstring, module-level setup. They went into the context verbatim, comments and
all, with no cap: tools.py sent 146 lines (~1,600 tokens) of preamble, mostly the
TOOLS schema the API already receives as tools=, with a 15-line helper. Selection
charged that whole preamble too, so one such file used half a medium budget and
crowded out the next file.
"""

import pytest

import llm_light.state as state
from llm_light import retrieval

SEP = "\n    // ...\n"


def _chunk(path, preamble, body):
    return f"// {path}\n{preamble}{SEP}{body}\n"


LONG_PREAMBLE = (
    '"""What this module is for."""\n'
    + "".join(f"import mod_{i}  # why mod_{i} is needed\n" for i in range(60))
    + "# a long comment block explaining the setup\n" * 5
    + "".join(f"CONST_{i} = {i}\n" for i in range(40))
).rstrip()
BODY = "def helper():\n    return 42"


@pytest.fixture
def store():
    saved = dict(state.chunk_store)
    state.chunk_store.clear()
    yield state.chunk_store
    state.chunk_store.clear()
    state.chunk_store.update(saved)


def _rendered(store, chunks):
    for cid, text in chunks.items():
        store[cid] = {"text": text}
    return retrieval._dedup_retrieved_context([(cid, 1.0) for cid in chunks])


def test_a_long_preamble_is_cut_and_says_so(store):
    out = _rendered(store, {"pkg/big.py::helper": _chunk("pkg/big.py", LONG_PREAMBLE, BODY)})
    head = out.split("    // helper")[0]
    assert len(head.splitlines()) <= 27            # path line + 25 lines + the note
    assert "import mod_0" in head and "CONST_39" not in head
    assert "more lines before the first symbol" in head
    assert "return 42" in out                      # the body is whole


def test_preamble_comments_are_stripped_like_the_body(store):
    out = _rendered(store, {"pkg/big.py::helper": _chunk("pkg/big.py", LONG_PREAMBLE, BODY)})
    assert "why mod_0 is needed" not in out
    assert '"""What this module is for."""' in out  # a docstring is not a comment


def test_a_short_preamble_without_comments_is_unchanged(store):
    text = _chunk("pkg/small.py", "import os\nimport re", BODY)
    out = _rendered(store, {"pkg/small.py::helper": text})
    assert out.startswith("// pkg/small.py\nimport os\nimport re\n")


def test_selection_charges_the_capped_preamble(store):
    text = _chunk("pkg/big.py", LONG_PREAMBLE, BODY)
    assert retrieval._rendered_tokens("pkg/big.py::helper", text, set()) < retrieval._estimate_tokens(text) / 2


def test_the_first_pass_charges_the_capped_preamble_too(store):
    # The second file is not a "strong" extra chunk (its score is well below the top),
    # so only the first, one-chunk-per-file pass can pick it: that pass must charge
    # the capped size too, or the first file's raw preamble crowds it out.
    a = _chunk("pkg/a.py", LONG_PREAMBLE, BODY)
    b = _chunk("pkg/b.py", LONG_PREAMBLE, "def other():\n    return 1")
    store.update({"pkg/a.py::helper": {"text": a}, "pkg/b.py::other": {"text": b}})
    budget = int(retrieval._estimate_tokens(a) * 1.2)
    picked = retrieval._select_within_budget([("pkg/a.py::helper", 0.9), ("pkg/b.py::other", 0.5)],
                                             budget, k=5)
    assert [cid for cid, _ in picked] == ["pkg/a.py::helper", "pkg/b.py::other"]


def test_a_big_preamble_no_longer_crowds_out_the_next_file(store):
    # Two files whose raw chunks overflow the budget together but fit once capped.
    a = _chunk("pkg/a.py", LONG_PREAMBLE, BODY)
    b = _chunk("pkg/b.py", LONG_PREAMBLE, "def other():\n    return 1")
    store.update({"pkg/a.py::helper": {"text": a}, "pkg/b.py::other": {"text": b}})
    budget = int(retrieval._estimate_tokens(a) * 1.2)
    picked = retrieval._select_within_budget([("pkg/a.py::helper", 0.9), ("pkg/b.py::other", 0.88)],
                                             budget, k=5)
    assert [cid for cid, _ in picked] == ["pkg/a.py::helper", "pkg/b.py::other"]
