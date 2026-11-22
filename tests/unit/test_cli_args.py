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
    ["--effort", "low", "hi"],             # not a flag of this CLI
    ["--budget", "1.0"],
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
