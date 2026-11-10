"""#211: the user's own hooks from ~/.llm_light/hooks.json. On a subscription the claude
CLI agent runs them (they go into its --settings file); with an API key llm_light runs
PreToolUse and PostToolUse itself, around its tools and its SEARCH/REPLACE edits, with
Claude Code's tool names so one matcher works in both modes."""

import io
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from llm_light import config, editor, hooks, permissions, tools
from llm_light.providers import claude as _claude_mod

_HOOK = '''import json, sys, time
log, code, message = sys.argv[1], int(sys.argv[2]), sys.argv[3]
sleep = float(sys.argv[4]) if len(sys.argv) > 4 else 0
event = json.loads(sys.stdin.read())
with open(log, "a", encoding="utf-8") as f:
    f.write(json.dumps(event) + "\\n")
time.sleep(sleep)
if message:
    print(message, file=sys.stderr)
sys.exit(code)
'''


@pytest.fixture
def hook(tmp_path, monkeypatch):
    """Write hooks.json; hook(event, matcher, code, message) adds one logging command."""
    script = tmp_path / "hook.py"
    script.write_text(_HOOK, encoding="utf-8")
    log = tmp_path / "hook.log"
    path = tmp_path / "home" / "hooks.json"
    monkeypatch.setenv("LLM_LIGHT_HOOKS_CONFIG", str(path))
    groups = {}

    def add(event, matcher=None, code=0, message="", sleep=0, timeout=None):
        command = (f'"{Path(sys.executable).as_posix()}" "{script.as_posix()}" '
                   f'"{log.as_posix()}" {code} "{message}" {sleep}')
        entry = {"type": "command", "command": command}
        if timeout is not None:
            entry["timeout"] = timeout
        group = {"hooks": [entry]}
        if matcher is not None:
            group["matcher"] = matcher
        groups.setdefault(event, []).append(group)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"hooks": groups}), encoding="utf-8")
        hooks.reset()

    def events():
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    add.events = events
    add.path = path
    hooks.reset()
    yield add
    hooks.reset()


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(root)
    permissions.set_mode(permissions.DEFAULT)
    return root


# --- the file -------------------------------------------------------------------------

def test_no_file_means_no_hooks(hook):
    assert hooks.load() == ({}, [])


def test_groups_are_kept_per_event(hook):
    hook("PreToolUse", "Read")
    hook("Stop")
    loaded, problems = hooks.load()
    assert problems == []
    assert set(loaded) == {"PreToolUse", "Stop"}
    assert loaded["PreToolUse"][0]["matcher"] == "Read"
    assert loaded["Stop"][0]["hooks"][0]["type"] == "command"


@pytest.mark.parametrize("raw", [
    {"hooks": {"NotAnEvent": [{"hooks": [{"type": "command", "command": "x"}]}]}},
    {"hooks": {"PreToolUse": [{"hooks": [{"type": "command"}]}]}},
    {"hooks": {"PreToolUse": [{"matcher": "(", "hooks": [{"type": "command", "command": "x"}]}]}},
    {"hooks": {"PreToolUse": "x"}},
    {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "x", "timeout": -1}]}]}},
])
def test_an_entry_that_does_not_fit_is_skipped_with_a_problem(hook, raw):
    hook.path.parent.mkdir(parents=True, exist_ok=True)
    hook.path.write_text(json.dumps(raw), encoding="utf-8")
    hooks.reset()
    loaded, problems = hooks.load()
    assert loaded == {} and len(problems) == 1


def test_a_project_settings_file_is_never_read(hook, project):
    # A cloned repo controls .claude/settings.json; its hooks would run commands.
    (project / ".claude").mkdir()
    (project / ".claude" / "settings.json").write_text(json.dumps(
        {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "evil"}]}]}}), encoding="utf-8")
    assert hooks.load() == ({}, [])


def test_default_file_is_under_the_home_directory(monkeypatch):
    monkeypatch.delenv("LLM_LIGHT_HOOKS_CONFIG", raising=False)
    assert hooks.config_file() == Path.home() / ".llm_light" / "hooks.json"


# --- the claude CLI agent (subscription) ------------------------------------------------

def _cli_settings(allowed=()):
    """The --settings file content of one CLI turn."""
    seen = {}
    proc = MagicMock()
    proc.stdout = iter(['{"type":"result","subtype":"success","result":"","usage":{}}\n'])
    proc.stderr = io.StringIO("")
    proc.wait.return_value = proc.poll.return_value = proc.returncode = 0

    def popen(cmd, **kwargs):
        seen["settings"] = json.loads(Path(cmd[cmd.index("--settings") + 1]).read_text(encoding="utf-8"))
        return proc
    with patch.object(config, "ALLOWED_COMMANDS", list(allowed)), \
         patch("llm_light.llm.subprocess.Popen", side_effect=popen), \
         patch("shutil.which", return_value="/fake/bin/claude"), \
         patch("sys.stdout", io.StringIO()):
        _claude_mod._make_cli_subprocess_call("hi")
    return seen["settings"]


def test_cli_turn_without_hooks_is_unchanged(hook):
    assert "hooks" not in _cli_settings()


def test_cli_turn_gets_the_users_hooks(hook):
    hook("PostToolUse", "Edit|Write")
    hook("Stop")
    settings = _cli_settings()
    assert settings["hooks"]["PostToolUse"][0]["matcher"] == "Edit|Write"
    assert "Stop" in settings["hooks"]


def test_the_shell_guard_runs_before_the_users_own_pre_tool_hooks(hook):
    # The guard blocks chained commands; a user hook must not be able to come first
    # and, say, rewrite the command it is about to check.
    hook("PreToolUse", "Bash")
    pre = _cli_settings(["python -m pytest"])["hooks"]["PreToolUse"]
    assert "cli_shell_guard" in pre[0]["hooks"][0]["command"]
    assert pre[1]["matcher"] == "Bash"


# --- API-key mode: around the tools -------------------------------------------------------

def test_pre_tool_use_sees_the_call_in_claude_codes_terms(hook, project):
    hook("PreToolUse", "Read")
    text, is_error = tools.run_tool("read_file", {"path": "a.py"}, root=project)
    assert not is_error and "x = 1" in text
    [event] = hook.events()
    assert event["hook_event_name"] == "PreToolUse"
    assert event["tool_name"] == "Read"
    assert Path(event["tool_input"]["file_path"]) == project / "a.py"
    assert Path(event["cwd"]) == project


def test_exit_2_blocks_the_call_and_tells_the_model_why(hook, project):
    hook("PreToolUse", "Read", code=2, message="no reading today")
    text, is_error = tools.run_tool("read_file", {"path": "a.py"}, root=project)
    assert is_error
    assert "no reading today" in text and "x = 1" not in text


def test_a_matcher_picks_the_tools(hook, project):
    hook("PreToolUse", "Grep|Glob", code=2, message="blocked")
    assert tools.run_tool("read_file", {"path": "a.py"}, root=project)[1] is False
    assert tools.run_tool("list_files", {}, root=project)[1] is True
    hook("PreToolUse", "", code=0)  # an empty matcher matches every tool
    tools.run_tool("read_file", {"path": "a.py"}, root=project)
    assert [e["tool_name"] for e in hook.events()][-1] == "Read"


def test_shell_and_mcp_tools_have_their_cli_names(hook, project):
    hook("PreToolUse", "Bash|mcp__.*", code=2, message="no")
    assert tools.run_tool("shell", {"command": "echo hi"}, root=project)[1] is True
    assert tools.run_tool("mcp__docs__search", {"q": "x"}, root=project)[1] is True
    names = [e["tool_name"] for e in hook.events()]
    assert names == ["Bash", "mcp__docs__search"]
    assert hook.events()[0]["tool_input"]["command"] == "echo hi"


def test_post_tool_use_gets_the_result_and_exit_2_reaches_the_model(hook, project):
    hook("PostToolUse", "Read", code=2, message="lint: a.py has issues")
    text, is_error = tools.run_tool("read_file", {"path": "a.py"}, root=project)
    assert "x = 1" in text and "lint: a.py has issues" in text
    [event] = hook.events()
    assert event["hook_event_name"] == "PostToolUse"
    assert "x = 1" in event["tool_response"]


def test_another_failure_does_not_block_and_is_noted(hook, project):
    hook("PreToolUse", "Read", code=1, message="hook broke")
    with patch("llm_light.hooks.notify") as notify:
        text, is_error = tools.run_tool("read_file", {"path": "a.py"}, root=project)
    assert not is_error and "x = 1" in text
    assert "hook broke" in notify.call_args.args[0]


def test_a_hook_past_its_timeout_is_stopped_and_does_not_block(hook, project):
    hook("PreToolUse", "Read", code=2, message="too late", sleep=20, timeout=1)
    with patch("llm_light.hooks.notify") as notify:
        text, is_error = tools.run_tool("read_file", {"path": "a.py"}, root=project)
    assert not is_error and "x = 1" in text
    assert "timed out" in notify.call_args.args[0]


# --- API-key mode: around SEARCH/REPLACE edits ----------------------------------------------

def test_edits_run_post_tool_use_before_the_commit(hook, project):
    # A formatter hook rewrites the file; the commit must hold the formatted text.
    fmt = project.parent / "fmt.py"
    fmt.write_text("import json, sys\nevent = json.loads(sys.stdin.read())\n"
                   "p = event['tool_input']['file_path']\n"
                   "open(p, 'a', encoding='utf-8').write('# formatted\\n')\n", encoding="utf-8")
    hook.path.parent.mkdir(parents=True, exist_ok=True)
    hook.path.write_text(json.dumps({"hooks": {"PostToolUse": [{"matcher": "Edit|Write", "hooks": [
        {"type": "command", "command": f'"{Path(sys.executable).as_posix()}" "{fmt.as_posix()}"'}]}]}}),
        encoding="utf-8")
    hooks.reset()
    edits = [{"path": "a.py", "type": "edit", "search": "x = 1\n", "replace": "x = 2\n"},
             {"path": "b.py", "type": "new", "content": "y = 1\n"}]
    committed = {}
    with patch.object(editor.git_manager, "is_git_repo", return_value=True), \
         patch.object(editor.git_manager, "auto_commit",
                      side_effect=lambda files, _e: committed.update(
                          {f: Path(f).read_text(encoding="utf-8") for f in files}) or False), \
         patch("sys.stdout", io.StringIO()):
        editor.apply_edits(edits, auto_apply=True)
    assert committed == {"a.py": "x = 2\n# formatted\n", "b.py": "y = 1\n# formatted\n"}


def test_edit_hooks_name_new_files_write_and_changed_ones_edit(hook, project):
    hook("PostToolUse", "Edit|Write")
    edits = [{"path": "a.py", "type": "edit", "search": "x = 1\n", "replace": "x = 2\n"},
             {"path": "b.py", "type": "new", "content": "y = 1\n"}]
    with patch.object(editor.git_manager, "is_git_repo", return_value=False), \
         patch("sys.stdout", io.StringIO()):
        editor.apply_edits(edits, auto_apply=True)
    names = {Path(e["tool_input"]["file_path"]).name: e["tool_name"] for e in hook.events()}
    assert names == {"a.py": "Edit", "b.py": "Write"}


# --- /hooks and /doctor ---------------------------------------------------------------------

def test_hooks_command_lists_them(hook):
    hook("PreToolUse", "Bash")
    text = "\n".join(hooks.command_lines())
    assert "PreToolUse" in text and "Bash" in text and "hook.py" in text


def test_hooks_is_a_listed_command():
    from llm_light import main
    assert "/hooks" in {cmd.split()[0] for cmd, _ in main._COMMANDS}


def test_doctor_counts_the_hooks_and_flags_a_broken_file(hook):
    from llm_light import doctor
    from llm_light.providers.base import NOTE, OK, PROBLEM
    rows = lambda: [(s, l) for s, l in doctor.checks() if l.startswith("Hooks")]
    assert rows()[0][0] == NOTE
    hook("PreToolUse", "Bash")
    assert rows()[0][0] == OK and "1" in rows()[0][1]
    hook.path.write_text("{bad", encoding="utf-8")
    hooks.reset()
    assert rows()[0][0] == PROBLEM
