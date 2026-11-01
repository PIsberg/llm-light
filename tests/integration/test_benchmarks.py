import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class TestRetrievalBenchmarkFixtureMode(unittest.TestCase):

    def test_load_fixture_instances_resolves_local_repo_paths(self):
        from tests.benchmarks import benchmark_retrieval as bench

        fixture_path = Path("tests/fixtures/retrieval_cases.json")
        instances = bench.load_fixture_instances(fixture_path)

        self.assertEqual(len(instances), 3)
        self.assertTrue(instances[0]["local_repo_path"].is_dir())
        self.assertEqual(instances[0]["gold_files"], ["src/auth.py"])

    def test_fixture_benchmark_outputs_non_zero_signal(self):
        from tests.benchmarks import benchmark_retrieval as bench

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "retrieval.json"
            cache_dir = Path(tmpdir) / "cache"

            argv = [
                "benchmark_retrieval.py",
                "--fixture", "tests/fixtures/retrieval_cases.json",
                "--cache-dir", str(cache_dir),
                "--output", str(output_path),
            ]
            with patch.object(bench.sys, "argv", argv):
                bench.main()

            results = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(len(results), 3)
        self.assertTrue(all(item["repo"] == "fixture/offline-demo" for item in results))
        self.assertGreater(
            sum(item["metrics"]["hit_at_k"]["10"] for item in results) / len(results),
            0.0,
        )
        self.assertTrue(all(item["metrics"]["mrr"] > 0.0 for item in results))


class TestRetrievalBenchmarkHybrid(unittest.TestCase):
    """#69: the benchmark ranks the hybrid index the way llm_light does."""

    IDS = ["a.py::f", "b.py::g", "c.py::h", "d.py::i"]

    def test_hybrid_ranks_by_fused_score_and_drops_chunks_past_the_gap(self):
        import numpy as np
        from llm_light import config
        from tests.benchmarks import benchmark_retrieval as bench
        fast = np.array([0.9, 0.5, 0.45, 0.1])
        dense = np.array([0.8, 0.6, 0.4, 0.1])
        pairs = bench.hybrid_rank(fast, dense, self.IDS, 10, config.DENSE_EMBED_MODEL)
        ids = [cid for cid, _ in pairs]
        self.assertEqual(ids[0], "a.py::f")
        self.assertNotIn("d.py::i", ids)  # far below the top fused score
        scores = [s for _, s in pairs]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertTrue(all(scores[0] - s <= config.HYBRID_SCORE_GAP for s in scores))

    def test_hybrid_retrieves_nothing_when_neither_model_clears_its_floor(self):
        import numpy as np
        from llm_light import config
        from tests.benchmarks import benchmark_retrieval as bench
        low = np.array([0.05, 0.04, 0.03, 0.02])
        self.assertEqual(bench.hybrid_rank(low, low, self.IDS, 10, config.DENSE_EMBED_MODEL), [])

    def test_static_alone_uses_the_fast_scores(self):
        import numpy as np
        from tests.benchmarks import benchmark_retrieval as bench
        fast = np.array([0.1, 0.9, 0.5, 0.45])
        pairs = bench.hybrid_rank(fast, None, self.IDS, 2, None)
        self.assertEqual(pairs[0][0], "b.py::g")
        self.assertLessEqual(len(pairs), 2)

    def test_script_runs_as_ci_runs_it(self):
        # CI calls `python tests/benchmarks/benchmark_retrieval.py`, where the
        # repo root is not on sys.path; an in-process main() cannot see that.
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "r.json"
            proc = subprocess.run(
                [sys.executable, "tests/benchmarks/benchmark_retrieval.py",
                 "--fixture", "tests/fixtures/retrieval_cases.json",
                 "--cache-dir", str(Path(tmpdir) / "cache"), "--output", str(out)],
                capture_output=True, text=True, timeout=300)
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            self.assertEqual(len(json.loads(out.read_text(encoding="utf-8"))), 3)

    def test_load_embedder_accepts_a_model_whose_max_seq_length_is_read_only(self):
        # The static model (StaticEmbedding) has no max_seq_length setter;
        # setting it crashed --retrieval static/hybrid on load.
        from tests.benchmarks import benchmark_retrieval as bench

        class ReadOnly:
            def __init__(self, *a, **k):
                pass

            @property
            def max_seq_length(self):
                return 10_000

        with patch.object(bench, "require_sentence_transformers", return_value=ReadOnly):
            self.assertIsInstance(bench._load_embedder("some/static-model"), ReadOnly)

    def test_retrieval_mode_flag_is_accepted(self):
        from tests.benchmarks import benchmark_retrieval as bench
        for mode in ("auto", "dense", "static", "hybrid"):
            with tempfile.TemporaryDirectory() as tmpdir,                  patch.object(bench, "run_benchmark") as run,                  patch.object(bench.sys, "argv", ["benchmark_retrieval.py", "--retrieval", mode,
                                                  "--cache-dir", tmpdir]):
                bench.main()
            self.assertEqual(run.call_args.args[0].retrieval, mode)


class TestRetrievalRegressionCheck(unittest.TestCase):

    def test_check_retrieval_fails_on_zero_signal_baseline(self):
        from tests.linting import check_regression as regression

        baseline = [
            {
                "repo": "fixture/offline-demo",
                "instance_id": "fixture-auth-login",
                "metrics": {
                    "hit_at_k": {"10": 0.0},
                    "recall_at_k": {"10": 0.0},
                },
            }
        ]

        with self.assertRaises(SystemExit):
            regression.check_retrieval(baseline, baseline)

    def test_check_retrieval_fails_on_quality_drop(self):
        from tests.linting import check_regression as regression

        baseline = [
            {
                "repo": "fixture/offline-demo",
                "instance_id": "fixture-auth-login",
                "metrics": {
                    "hit_at_k": {"10": 1.0},
                    "recall_at_k": {"10": 1.0},
                },
            }
        ]
        current = [
            {
                "repo": "fixture/offline-demo",
                "instance_id": "fixture-auth-login",
                "metrics": {
                    "hit_at_k": {"10": 0.0},
                    "recall_at_k": {"10": 0.0},
                },
            }
        ]

        with self.assertRaises(SystemExit):
            regression.check_retrieval(baseline, current)


if __name__ == "__main__":
    unittest.main()
