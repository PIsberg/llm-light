"""Unknown command-line flags are refused, not dropped (#321).

main() used parse_known_args() and never looked at what it did not know, so a
mistyped flag vanished: `--plan` started in the writable default mode instead
of read-only plan mode, and `--modle haiku "hi"` sent "haiku hi" as a billed
question. Each case here must stop with a usage error before any turn runs.
"""

import sys
from unittest.mock import MagicMock

import pytest

import llm_light.main as main_mod


def _setup(monkeypatch, argv):
    fake = MagicMock()
    fake.has.return_value = True
    monkeypatch.setattr(sys, "argv", ["llm_light"] + argv)
    monkeypatch.setattr(main_mod.providers, "select", MagicMock(return_value=fake))
    one_shot, start_chat = MagicMock(), MagicMock()
    monkeypatch.setattr(main_mod, "one_shot", one_shot)
    monkeypatch.setattr(main_mod, "continue_turn", MagicMock(return_value=True))
    monkeypatch.setattr(main_mod, "start_chat", start_chat)
    monkeypatch.setattr(main_mod, "_start_llmlingua_load", lambda: None)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    return one_shot, start_chat


def _main(monkeypatch, argv):
    one_shot, start_chat = _setup(monkeypatch, argv)
    main_mod.main()
    return one_shot, start_chat


def _refused(monkeypatch, capsys, argv):
    """Run main() on argv; assert it exits 2 before any turn or chat. Returns stderr."""
    one_shot, start_chat = _setup(monkeypatch, argv)
    with pytest.raises(SystemExit) as exc:
        main_mod.main()
    assert exc.value.code == 2, f"{argv} was accepted"
    one_shot.assert_not_called()
    start_chat.assert_not_called()
    return capsys.readouterr().err


@pytest.mark.parametrize("argv", [
    ["--plan", "explain auth"],            # meant --permission-mode plan
    ["--modle", "haiku", "hi"],            # the value became part of the question
    ["--temperature", "0.2", "hi"],        # not a flag of this CLI
    ["--max-cost", "1.0"],
])
def test_an_unknown_flag_is_a_usage_error(monkeypatch, capsys, argv):
    err = _refused(monkeypatch, capsys, argv)
    assert argv[0] in err


def test_a_close_typo_names_the_flag_it_meant(monkeypatch, capsys):
    err = _refused(monkeypatch, capsys, ["--modle", "haiku", "hi"])
    assert "--model" in err


def test_the_message_says_how_to_ask_a_question_that_starts_with_a_dash(monkeypatch, capsys):
    err = _refused(monkeypatch, capsys, ["-x"])
    assert "--" in err


def test_a_question_after_double_dash_may_start_with_a_dash(monkeypatch):
    one_shot, _ = _main(monkeypatch, ["--", "-x: what does it do?"])
    assert one_shot.call_args.args[0] == "-x: what does it do?"


def test_known_flags_still_work(monkeypatch):
    one_shot, _ = _main(monkeypatch, ["--model", "haiku", "what", "is", "this"])
    assert one_shot.call_args.args[0] == "what is this"


# --- --effort and --budget (#327) -------------------------------------------------------

import json

import llm_light.state as state
from llm_light.providers import base


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.setattr(state, "pinned_effort", None)
    monkeypatch.setattr(state, "session_budget", None)


def _seen_at_turn(monkeypatch, argv, has=lambda cap: True):
    """Run main() on argv; return (pinned_effort, session_budget) as the turn saw them."""
    seen = {}
    one_shot, _ = _setup(monkeypatch, argv)
    one_shot.side_effect = lambda *a, **k: seen.update(effort=state.pinned_effort,
                                                       budget=state.session_budget)
    main_mod.providers.select.return_value.has.side_effect = has
    main_mod.main()
    return seen.get("effort"), seen.get("budget")


def test_effort_flag_pins_the_effort(monkeypatch, clean_state):
    effort, _ = _seen_at_turn(monkeypatch, ["--effort", "low", "-p", "what is this"])
    assert effort == "low"


def test_effort_flag_beats_the_settings_file(monkeypatch, clean_state, tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"effort": "max"}), encoding="utf-8")
    monkeypatch.setenv("LLM_LIGHT_SETTINGS_FILE", str(settings))
    effort, _ = _seen_at_turn(monkeypatch, ["--effort", "low", "-p", "q"])
    assert effort == "low"


def test_an_unknown_effort_is_a_usage_error(monkeypatch, capsys, clean_state):
    err = _refused(monkeypatch, capsys, ["--effort", "huge", "-p", "q"])
    assert "--effort" in err


def test_effort_is_ignored_with_a_notice_where_it_does_not_exist(monkeypatch, capsys, clean_state):
    effort, _ = _seen_at_turn(monkeypatch, ["--effort", "low", "-p", "q"],
                              has=lambda cap: cap != base.EFFORT)
    assert effort is None
    assert "--effort ignored" in capsys.readouterr().err


@pytest.mark.parametrize("value,expected", [("0.50", 0.5), ("$2", 2.0)])
def test_budget_flag_sets_the_cap(monkeypatch, clean_state, value, expected):
    _, budget = _seen_at_turn(monkeypatch, ["--budget", value, "-p", "q"])
    assert budget == expected


@pytest.mark.parametrize("value", ["0", "-1", "inf", "nan", "abc"])
def test_a_bad_budget_is_a_usage_error(monkeypatch, capsys, clean_state, value):
    err = _refused(monkeypatch, capsys, ["--budget", value, "-p", "q"])
    assert "--budget" in err


def test_budget_is_ignored_with_a_notice_without_dollar_pricing(monkeypatch, capsys, clean_state):
    _, budget = _seen_at_turn(monkeypatch, ["--budget", "1", "-p", "q"],
                              has=lambda cap: cap != base.USD_BUDGET)
    assert budget is None
    assert "--budget ignored" in capsys.readouterr().err
