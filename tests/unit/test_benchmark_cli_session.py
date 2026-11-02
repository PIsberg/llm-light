"""The session benchmark's arithmetic: a wrong mean would publish a wrong saving."""
import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "benchmarks" / "benchmark_cli_session.py"
_SPEC = importlib.util.spec_from_file_location("benchmark_cli_session", _PATH)
bench = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bench)


def _row(arm, session="routing", repeat=0, **kw):
    base = {"arm": arm, "session": session, "repeat": repeat, "cost": 0.0, "input": 0, "cache_read": 0,
            "cache_write": 0, "output": 0, "tools": 0, "seconds": 0.0, "error": False}
    return {**base, **kw}


def test_means_are_per_session_not_per_turn():
    rows = [_row("resume", cost=0.1, cache_write=100), _row("resume", cost=0.3, cache_write=300),
            _row("resume", repeat=1, cost=0.2, cache_write=200)]
    s = bench.summarize(rows)["resume"]
    assert (s["sessions"], s["turns"]) == (2, 3)
    assert abs(s["cost"] - 0.3) < 1e-9
    assert s["cache_write"] == 300


def test_each_arm_is_counted_alone():
    s = bench.summarize([_row("resume", cost=0.1), _row("flat", cost=0.5)])
    assert abs(s["resume"]["cost"] - 0.1) < 1e-9
    assert abs(s["flat"]["cost"] - 0.5) < 1e-9


def test_a_failed_turn_is_counted():
    assert bench.summarize([_row("flat", error=True), _row("flat")])["flat"]["failed"] == 1


def test_verdicts_name_the_arm_that_won():
    rows = [{"judged": True, "outcome": o} for o in ("x", "x", "y", "tie", "invalid")]
    assert bench.summarize(rows)["judged"] == {"resume_wins": 2, "flat_wins": 1, "ties": 1, "invalid": 1}


def test_the_long_session_passes_the_history_limit():
    # Otherwise it would not exercise compression ending the CLI session.
    from llm_light import config
    assert len(bench.SESSIONS["long"]) > config.MAX_HISTORY_TURNS


def test_it_refuses_an_api_key(monkeypatch, capsys):
    monkeypatch.setattr(bench.config, "AUTH_MODE", "API_KEY")
    assert bench.main(["--repeats", "1"]) == 2
    assert "subscription" in capsys.readouterr().err
