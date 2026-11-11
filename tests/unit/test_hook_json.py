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
