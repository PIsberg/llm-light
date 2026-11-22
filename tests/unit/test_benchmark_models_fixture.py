"""The held-out question set and the --fixture option of tests/benchmarks/benchmark_models.py (#287).

Floors swept on repo_questions.json fit the thresholds to that answer key, so a change is
checked against a second set the sweep never saw. These tests keep that set honest (no
question shared with the tuning set, every answer file real) and check that switching sets
does not re-embed the repository, which takes minutes with a dense model.
"""
import importlib.util
import json
import sys
import types
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parents[2]
_BENCH = _ROOT / "tests" / "benchmarks" / "benchmark_models.py"
_TUNING = _ROOT / "tests" / "fixtures" / "repo_questions.json"
_HELDOUT = _ROOT / "tests" / "fixtures" / "repo_questions_heldout.json"


@pytest.fixture
def bench():
    spec = importlib.util.spec_from_file_location("benchmark_models", _BENCH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _questions(path):
    return json.loads(path.read_text(encoding="utf-8"))["questions"]


def test_heldout_shares_no_question_with_the_tuning_set():
    tuning = {q["question"].strip().lower() for q in _questions(_TUNING)}
    shared = [q["question"] for q in _questions(_HELDOUT) if q["question"].strip().lower() in tuning]
    assert shared == []


def test_heldout_answer_files_exist_and_kinds_are_known():
    for q in _questions(_HELDOUT):
        assert q["kind"] in {"identifier", "paraphrase", "unrelated"}, q
        if q["kind"] == "unrelated":
            assert q["file"] is None, q
        else:
            assert (_ROOT / q["file"]).is_file(), q


def test_heldout_has_enough_off_topic_and_paraphrase_questions():
    kinds = [q["kind"] for q in _questions(_HELDOUT)]
    # The tuning set has 6 off-topic and 12 paraphrases; the check needs at least as many.
    assert kinds.count("unrelated") >= 10
    assert kinds.count("paraphrase") >= 12


def test_main_reads_the_fixture_given(bench):
    assert bench.parse_args([]).fixture == bench.FIXTURE
    assert bench.parse_args(["--fixture", str(_HELDOUT)]).fixture == _HELDOUT


class _FakeModel:
    calls = []

    def __init__(self, name, device=None):
        pass

    def encode(self, texts, normalize_embeddings=True, batch_size=None):
        _FakeModel.calls.append(list(texts))
        return np.ones((len(texts), 3), dtype=np.float32)


def test_switching_question_sets_does_not_re_embed_the_chunks(bench, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "sentence_transformers",
                        types.SimpleNamespace(SentenceTransformer=_FakeModel))
    _FakeModel.calls = []
    chunks = [{"id": "a.py::f", "text": "def f(): pass"}, {"id": "b.py::g", "text": "def g(): pass"}]
    first = [{"question": "where is f"}]
    second = [{"question": "where is g"}, {"question": "hi"}]

    E1, Q1 = bench.embed("all-mpnet-base-v2", chunks, first, tmp_path)
    E2, Q2 = bench.embed("all-mpnet-base-v2", chunks, second, tmp_path)
    E3, Q3 = bench.embed("all-mpnet-base-v2", chunks, first, tmp_path)

    encoded = [len(c) for c in _FakeModel.calls]
    # Chunks once, then each question set once; the third call is all cache.
    assert encoded == [2, 1, 2]
    assert E1.shape == E2.shape == E3.shape == (2, 3)
    assert Q1.shape == Q3.shape == (1, 3) and Q2.shape == (2, 3)


def test_an_edited_chunk_is_re_embedded(bench, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "sentence_transformers",
                        types.SimpleNamespace(SentenceTransformer=_FakeModel))
    _FakeModel.calls = []
    qs = [{"question": "where is f"}]
    bench.embed("all-mpnet-base-v2", [{"id": "a.py::f", "text": "def f(): pass"}], qs, tmp_path)
    bench.embed("all-mpnet-base-v2", [{"id": "a.py::f", "text": "def f(): return 1"}], qs, tmp_path)

    # Same id, new text: the stale vector must not be reused (questions stay cached).
    assert [len(c) for c in _FakeModel.calls] == [1, 1, 1]
