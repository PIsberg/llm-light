"""Selection charges each chunk what the renderer shows of it (#329).

Only the first chunks get their code in the context (none at low effort, one at
medium, two at high and max); every other selected chunk is one summary line, about
25 tokens. Selection charged every chunk its full body, so at low effort one
1,400-token function used the whole 1,500-token budget and the context named one
file where six would have fitted.
"""

from unittest.mock import MagicMock

import numpy as np
import pytest

import llm_light as cl
import llm_light.state as state
from llm_light import retrieval

SEP = "\n    // ...\n"


def _big_chunk(path, name, lines=200):
    body = f"def {name}():\n" + "".join(f"    value_{i} = compute_{i}(value_{i - 1})\n" for i in range(lines))
    return f"// {path}\nimport os{SEP}{body}"


@pytest.fixture
def store():
    saved = (dict(cl.chunk_store), cl.TOP_K, cl.EMBED_MODEL, cl.embedder, cl.MIN_SCORE,
             cl.RELATIVE_SCORE_FLOOR)
    cl.chunk_store.clear()
    for i in range(6):
        emb = np.zeros(8)
        emb[0], emb[i + 1 if i < 7 else 7] = 1.0, 0.3 + 0.05 * i
        cl.chunk_store[f"pkg/mod_{i}.py::func_{i}"] = {
            "text": _big_chunk(f"pkg/mod_{i}.py", f"func_{i}"), "emb": emb / np.linalg.norm(emb)}
    cl.TOP_K = 24                      # k = 6 at a 1,500-token budget
    cl.EMBED_MODEL = "all-MiniLM-L6-v2"
    cl.MIN_SCORE = 0.0
    cl.RELATIVE_SCORE_FLOOR = 0.0
    embedder = MagicMock()
    q = np.zeros(8)
    q[0] = 1.0
    embedder.encode.return_value = q
    cl.embedder = embedder
    state.embedder_ready.set()
    yield
    cl.chunk_store.clear()
    cl.chunk_store.update(saved[0])
    (_, cl.TOP_K, cl.EMBED_MODEL, cl.embedder, cl.MIN_SCORE, cl.RELATIVE_SCORE_FLOOR) = saved
    state.embedder_ready.clear()


def test_the_chunks_are_bigger_than_the_low_budget():
    # The premise: one chunk alone costs most of a 1,500-token budget in full.
    assert retrieval._estimate_tokens(_big_chunk("pkg/mod_0.py", "func_0")) > 1_200


def test_low_effort_names_every_file_that_fits_as_a_summary(store):
    ctx, hits = cl.retrieve("q", token_budget=1_500, effort="low")
    assert len(hits) == 6
    assert all(f"pkg/mod_{i}.py" in ctx for i in range(6))


@pytest.mark.parametrize("effort,budget", [("low", 1_500), ("medium", 3_000), ("high", 6_000), ("max", 9_000)])
def test_the_rendered_context_stays_within_the_budget(store, effort, budget):
    ctx, _ = cl.retrieve("q", token_budget=budget, effort=effort)
    assert retrieval._estimate_tokens(ctx) <= budget


def test_medium_effort_still_details_the_top_chunk_and_lists_the_rest(store):
    ctx, hits = cl.retrieve("q", token_budget=3_000, effort="medium")
    assert "Detailed Code Context" in ctx and "value_199" in ctx   # the top chunk in full
    assert len(hits) == 6 and all(f"pkg/mod_{i}.py" in ctx for i in range(6))


def test_without_an_effort_selection_charges_full_bodies_as_before(store):
    # Callers that pass no detail count (benchmarks, older tests) keep the old charge.
    candidates = [(cid, 1.0 - n / 100) for n, cid in enumerate(cl.chunk_store)]
    assert len(retrieval._select_within_budget(candidates, 1_500, k=6)) < 6
