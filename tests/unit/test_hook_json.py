"""#227: with an API key, a hook's JSON on stdout counts, as Claude Code reads it.

Claude Code reads a JSON object on stdout on any exit code but 2, which blocks whatever the
JSON says (code.claude.com/docs/en/hooks, "JSON output", read 2026-11-11). The examples here
are the documented ones: PreToolUse decides in hookSpecificOutput.permissionDecision (the
older top-level decision "block"/"approve" still maps to deny/allow), PostToolUse with a
top-level decision "block" and its reason, and additionalContext and systemMessage on both.
"""

import json
import sys
from pathlib import Path

import pytest

from llm_light import hooks, tools

_PRINT = '''import sys
sys.stdin.read()
sys.stdout.write(open(sys.argv[1], encoding="utf-8").read())
sys.exit(int(sys.argv[2]))
'''


@pytest.fixture
def json_hook(tmp_path, monkeypatch):
    """json_hook(event, output, code=0) adds a hook that prints `output` (a dict is JSON)."""
    script = tmp_path / "print.py"
    script.write_text(_PRINT, encoding="utf-8")
    path = tmp_path / "home" / "hooks.json"
    monkeypatch.setenv("LLM_LIGHT_HOOKS_CONFIG", str(path))
    groups = {}

    def add(event, output, code=0):
        out = tmp_path / f"out{sum(len(g) for g in groups.values())}.txt"
        out.write_text(output if isinstance(output, str) else json.dumps(output), encoding="utf-8")
        command = f'"{Path(sys.executable).as_posix()}" "{script.as_posix()}" "{out.as_posix()}" {code}'
        groups.setdefault(event, []).append({"hooks": [{"type": "command", "command": command}]})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"hooks": groups}), encoding="utf-8")
        hooks.reset()
    hooks.reset()
    yield add
    hooks.reset()


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def notices(monkeypatch):
    said = []
    monkeypatch.setattr(hooks, "notify", lambda text, error=False: said.append((text, error)))
    return said


def _read(project):
    return tools.run_tool("read_file", {"path": "a.py"}, root=project)


def _pre(decision, reason="policy says no"):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": decision,
                                   "permissionDecisionReason": reason}}


def test_permission_decision_deny_blocks_and_tells_the_model_why(json_hook, project):
    json_hook("PreToolUse", _pre("deny"))
    out, is_error = _read(project)
    assert is_error and "policy says no" in out and "x = 1" not in out


def test_the_older_decision_block_still_blocks(json_hook, project):
    json_hook("PreToolUse", {"decision": "block", "reason": "old style no"})
    out, is_error = _read(project)
    assert is_error and "old style no" in out


def test_ask_is_denied_since_nothing_can_be_asked_mid_turn(json_hook, project):
    # As in a claude -p run: a call that needs a confirmation no one can give is refused.
    json_hook("PreToolUse", _pre("ask", "are you sure?"))
    out, is_error = _read(project)
    assert is_error and "are you sure?" in out


@pytest.mark.parametrize("decision", ["allow", "approve"])
def test_allow_runs_the_call(json_hook, project, decision):
    json_hook("PreToolUse", _pre(decision) if decision == "allow"
              else {"decision": "approve", "reason": "fine"})
    out, is_error = _read(project)
    assert not is_error and "x = 1" in out


def test_exit_2_blocks_whatever_the_json_says(json_hook, project):
    json_hook("PreToolUse", _pre("allow"), code=2)
    assert _read(project)[1] is True


def test_json_on_another_exit_code_decides_without_a_failure_notice(json_hook, project, notices):
    json_hook("PreToolUse", _pre("deny"), code=1)
    assert _read(project)[1] is True
    assert not any(error for _, error in notices)


def test_pre_tool_additional_context_reaches_the_model_with_the_result(json_hook, project):
    json_hook("PreToolUse", {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "additionalContext": "Current environment: production."}})
    out, is_error = _read(project)
    assert not is_error and "x = 1" in out and "Current environment: production." in out


def test_post_tool_block_puts_the_reason_next_to_the_result(json_hook, project):
    json_hook("PostToolUse", {"decision": "block", "reason": "This file is generated."})
    out, is_error = _read(project)
    assert "x = 1" in out and "This file is generated." in out


def test_post_tool_additional_context_reaches_the_model(json_hook, project):
    json_hook("PostToolUse", {"hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext": "Edit src/schema.ts and run `bun generate` instead."}})
    out, _ = _read(project)
    assert "x = 1" in out and "bun generate" in out


def test_system_message_is_shown_to_the_user_not_the_model(json_hook, project, notices):
    json_hook("PostToolUse", {"systemMessage": "Formatter skipped a file"})
    out, _ = _read(project)
    assert "Formatter skipped" not in out
    assert ("PostToolUse hook: Formatter skipped a file", False) in notices


def test_plain_text_on_stdout_is_ignored_as_before(json_hook, project, notices):
    json_hook("PostToolUse", "formatted 1 file\n")
    out, is_error = _read(project)
    assert not is_error and out.strip().endswith("x = 1") and notices == []


def test_broken_json_is_noted_and_does_not_block(json_hook, project, notices):
    json_hook("PreToolUse", '{"hookSpecificOutput": ')
    out, is_error = _read(project)
    assert not is_error and "x = 1" in out
    assert any("not valid JSON" in text and error for text, error in notices)



# --- #248: updatedInput, updatedToolOutput, continue ------------------------------------------

def test_updated_input_redirects_the_call(json_hook, project, notices):
    (project / "b.py").write_text("y = 2\n", encoding="utf-8")
    json_hook("PreToolUse", {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                   "permissionDecision": "allow",
                                                   "updatedInput": {"file_path": str(project / "b.py")}}})
    output, is_error = _read(project)
    assert not is_error
    assert "y = 2" in output and "x = 1" not in output


def test_updated_input_cannot_reach_past_the_project(json_hook, project, tmp_path, notices):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOKEN=abc\n", encoding="utf-8")
    json_hook("PreToolUse", {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                   "updatedInput": {"file_path": str(secret)}}})
    output, is_error = _read(project)
    assert is_error
    assert "TOKEN" not in output
    assert "outside" in output


def test_updated_tool_output_replaces_the_result(json_hook, project, notices):
    json_hook("PostToolUse", {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                                    "updatedToolOutput": "[redacted by policy]"}})
    output, is_error = _read(project)
    assert output == "[redacted by policy]"
    assert "x = 1" not in output


def test_continue_false_stops_the_turn(json_hook, project, notices):
    json_hook("PreToolUse", {"continue": False, "stopReason": "build is frozen"})
    with pytest.raises(KeyboardInterrupt):
        _read(project)
    assert any("build is frozen" in text for text, _ in notices)


def test_continue_false_after_an_edit_does_not_stop_the_commit(json_hook, project, notices):
    # The edit is already on disk; hooks.after_edit must return so the commit still runs.
    json_hook("PostToolUse", {"continue": False, "stopReason": "freeze"})
    hooks.after_edit("a.py", new=False)  # no KeyboardInterrupt



# --- #226: UserPromptSubmit, Stop and PreCompact with an API key -----------------------------

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


@pytest.fixture
def api_turn(monkeypatch, project):
    """Run llm.chat with only the provider's complete faked; returns the requests sent."""
    from llm_light import llm
    from llm_light.providers.claude import ClaudeProvider
    import llm_light.state as state
    sent = []

    def complete(self, request):
        sent.append(request)
        return "Fine.", None, True

    monkeypatch.setattr(ClaudeProvider, "complete", complete)
    monkeypatch.setattr(ClaudeProvider, "turn_done", lambda self: None)
    monkeypatch.setattr(llm, "_agent_oauth", lambda: False)
    monkeypatch.setattr(llm, "_record_usage", lambda *a, **k: False)
    saved = (list(state.conversation_history), state.turn_count, list(state.checkpoints))
    yield sent
    state.conversation_history[:], state.turn_count, state.checkpoints[:] = saved


def test_user_prompt_submit_exit_2_refuses_the_question(record_hook, api_turn, capsys):
    from llm_light import llm
    log = record_hook("UserPromptSubmit", stderr="no questions about payroll", code=2)
    assert llm.chat("what does payroll.py do", use_index=False) is False
    assert api_turn == []                                   # nothing reached the model
    assert "no questions about payroll" in capsys.readouterr().out
    assert json.loads(log.read_text(encoding="utf-8"))["prompt"] == "what does payroll.py do"


def test_user_prompt_submit_stdout_goes_with_the_question(record_hook, api_turn):
    from llm_light import llm
    record_hook("UserPromptSubmit", stdout="Ticket ABC-12 is in progress.")
    assert llm.chat("what is next", use_index=False)
    assert "Ticket ABC-12 is in progress." in json.dumps(api_turn[0]["messages"])


def test_stop_runs_after_a_turn(record_hook, api_turn):
    from llm_light import llm
    log = record_hook("Stop")
    assert llm.chat("hello", use_index=False)
    assert json.loads(log.read_text(encoding="utf-8"))["hook_event_name"] == "Stop"


def test_pre_compact_runs_before_compact(record_hook, project, monkeypatch):
    from llm_light import llm
    import llm_light.state as state
    from llm_light.providers.claude import ClaudeProvider
    log = record_hook("PreCompact")
    monkeypatch.setattr(ClaudeProvider, "summarize", lambda self, text: ("summary", None))
    monkeypatch.setattr(llm, "_agent_oauth", lambda: False)
    monkeypatch.setattr(llm, "_record_usage", lambda *a, **k: False)
    saved = list(state.conversation_history)
    try:
        state.conversation_history[:] = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
        llm.compact_history("keep the codeword")
    finally:
        state.conversation_history[:] = saved
    payload = json.loads(log.read_text(encoding="utf-8"))
    assert (payload["trigger"], payload["custom_instructions"]) == ("manual", "keep the codeword")
