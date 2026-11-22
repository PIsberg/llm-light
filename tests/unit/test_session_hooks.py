"""#311: SessionStart and SessionEnd hooks with an API key.

On a subscription the claude CLI runs them. With an API key llm_light runs them itself, with
Claude Code's payload: SessionStart (source startup, resume or clear) when a conversation
starts, its output going with the next question; SessionEnd (reason clear,
prompt_input_exit or other) when one ends. The hooks here are real processes that record
the payload they received.
"""
import json
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import llm_light.state as state
from llm_light import hooks

_RECORD = '''import json, sys
payload = json.loads(sys.stdin.read())
open(sys.argv[1], "a", encoding="utf-8").write(json.dumps(payload) + "\\n")
sys.stdout.write(sys.argv[2])
sys.stderr.write(sys.argv[3])
sys.exit(int(sys.argv[4]))
'''


@pytest.fixture
def record_hook(tmp_path, monkeypatch):
    """record_hook(event, stdout="", stderr="", code=0) -> file holding each payload received."""
    script = tmp_path / "record.py"
    script.write_text(_RECORD, encoding="utf-8")
    path = tmp_path / "home" / "hooks.json"
    monkeypatch.setenv("LLM_LIGHT_HOOKS_CONFIG", str(path))
    groups = {}

    def add(event, stdout="", stderr="", code=0):
        log = tmp_path / f"{event}.jsonl"
        command = (f'"{Path(sys.executable).as_posix()}" "{script.as_posix()}" "{log.as_posix()}" '
                   f'"{stdout}" "{stderr}" {code}')
        groups.setdefault(event, []).append({"hooks": [{"type": "command", "command": command}]})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"hooks": groups}), encoding="utf-8")
        hooks.reset()
        return log
    hooks.reset()
    yield add
    hooks.reset()


def _payloads(log):
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def api_key(monkeypatch, tmp_path):
    import llm_light as cl
    from llm_light import llm
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(llm, "_agent_oauth", lambda: False)
    monkeypatch.setattr(cl, "_agent_oauth", lambda: False, raising=False)


def _loop(inputs, **chat_kwargs):
    """Run start_chat over typed lines; an exception instance in `inputs` is raised there."""
    import llm_light as cl
    feed = iter(inputs)

    def typed(_prompt=">"):
        line = next(feed)
        if isinstance(line, BaseException):
            raise line
        return line

    chat = MagicMock(return_value=True)
    real_thread = threading.Thread

    def thread(*args, **kwargs):
        # Fake llm_light's background threads (indexing, watcher, heartbeat), as the
        # other start_chat tests do, but not subprocess's: on Windows communicate()
        # reads a hook's output on threads.
        target = kwargs.get("target")
        if str(kwargs.get("name", "")).startswith("llm-light") or \
                getattr(target, "__name__", "") == "heartbeat":
            return MagicMock()
        return real_thread(*args, **kwargs)

    with patch.object(cl, "full_refresh"), \
         patch("llm_light.Observer", return_value=MagicMock()), \
         patch("llm_light.SourceHandler", return_value=MagicMock()), \
         patch("threading.Thread", side_effect=thread), \
         patch.object(cl, "_PROMPTTK_AVAILABLE", False), \
         patch.object(cl, "print_session_summary"), \
         patch.object(cl, "chat", chat), \
         patch.object(cl.main.session, "save"), \
         patch.object(cl.main.session, "clear"), \
         patch.object(cl.main.session, "restore", return_value="Restored."), \
         patch("builtins.input", side_effect=typed), \
         patch("builtins.print"):
        try:
            cl.start_chat(**chat_kwargs)
        finally:
            state.stop_event.clear()
            state.pending_context.clear()
    return chat


def test_session_start_output_goes_with_the_first_question(record_hook, api_key):
    log = record_hook("SessionStart", stdout="Sprint 12: payments are frozen.")
    chat = _loop(["hello", "exit"])
    assert "Sprint 12: payments are frozen." in chat.call_args.kwargs["context"]
    assert [p["source"] for p in _payloads(log)] == ["startup"]
    assert _payloads(log)[0]["hook_event_name"] == "SessionStart"


def test_session_start_output_is_sent_once(record_hook, api_key):
    record_hook("SessionStart", stdout="Sprint 12")
    chat = _loop(["one", "two", "exit"])
    first, second = chat.call_args_list
    assert "Sprint 12" in first.kwargs["context"]
    assert "Sprint 12" not in second.kwargs.get("context", "")


def test_session_start_source_is_resume_for_continue(record_hook, api_key):
    log = record_hook("SessionStart")
    _loop(["exit"], resume=True)
    assert [p["source"] for p in _payloads(log)] == ["resume"]


def test_clear_ends_the_conversation_and_starts_another(record_hook, api_key):
    start = record_hook("SessionStart", stdout="fresh context")
    end = record_hook("SessionEnd")
    chat = _loop(["/clear", "after clear", "exit"])
    assert [p["source"] for p in _payloads(start)] == ["startup", "clear"]
    assert [p["reason"] for p in _payloads(end)] == ["clear", "prompt_input_exit"]
    assert "fresh context" in chat.call_args.kwargs["context"]


@pytest.mark.parametrize("how", ["exit", EOFError(), KeyboardInterrupt()])
def test_session_end_when_the_user_leaves_the_prompt(record_hook, api_key, how):
    end = record_hook("SessionEnd")
    _loop([how])
    assert [p["reason"] for p in _payloads(end)] == ["prompt_input_exit"]


def test_session_end_reason_is_other_on_sigterm(record_hook, api_key):
    end = record_hook("SessionEnd")
    with pytest.raises(SystemExit):
        _loop([SystemExit(143)])  # _setup_signal_handlers turns SIGTERM into this
    assert [p["reason"] for p in _payloads(end)] == ["other"]


def test_session_start_matcher_selects_the_source(record_hook, api_key, tmp_path, monkeypatch):
    log = record_hook("SessionStart")
    path = Path(hooks.config_file())
    config = json.loads(path.read_text(encoding="utf-8"))
    config["hooks"]["SessionStart"][0]["matcher"] = "clear"
    path.write_text(json.dumps(config), encoding="utf-8")
    hooks.reset()
    _loop(["/clear", "exit"])
    assert [p["source"] for p in _payloads(log)] == ["clear"]


def test_session_hooks_do_not_run_on_a_subscription(record_hook, monkeypatch, tmp_path):
    import llm_light as cl
    from llm_light import llm
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(llm, "_agent_oauth", lambda: True)
    monkeypatch.setattr(cl, "_agent_oauth", lambda: True, raising=False)
    start, end = record_hook("SessionStart"), record_hook("SessionEnd")
    _loop(["exit"])
    # The claude CLI agent runs them there; running them here too would run them twice.
    assert _payloads(start) == [] and _payloads(end) == []


def _main(monkeypatch, argv):
    from llm_light import main as main_mod
    fake = MagicMock()
    fake.has.return_value = True
    monkeypatch.setattr(sys, "argv", ["llm_light"] + argv)
    monkeypatch.setattr(main_mod.providers, "select", MagicMock(return_value=fake))
    one_shot = MagicMock(return_value=True)
    monkeypatch.setattr(main_mod, "one_shot", one_shot)
    monkeypatch.setattr(main_mod, "start_chat", MagicMock())
    monkeypatch.setattr(main_mod, "_start_llmlingua_load", lambda: None)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    main_mod.main()
    return one_shot


def test_one_shot_runs_session_start_and_end(record_hook, api_key, monkeypatch):
    start = record_hook("SessionStart", stdout="on-call: Dana")
    end = record_hook("SessionEnd")
    one_shot = _main(monkeypatch, ["who is on call?"])
    assert "on-call: Dana" in one_shot.call_args.kwargs["context"]
    assert [p["source"] for p in _payloads(start)] == ["startup"]
    assert [p["reason"] for p in _payloads(end)] == ["other"]


def test_session_start_exit_2_is_shown_and_does_not_stop_the_session(record_hook, api_key, capsys):
    record_hook("SessionStart", stderr="could not reach the tracker", code=2)
    chat = _loop(["hello", "exit"])
    chat.assert_called_once()
