"""The user's own hooks, from ~/.llm_light/hooks.json (#211).

The file has the shape of the `hooks` setting of Claude Code:

    {"hooks": {
        "PostToolUse": [{"matcher": "Edit|Write",
                         "hooks": [{"type": "command", "command": "ruff format --quiet", "timeout": 30}]}],
        "PreToolUse":  [{"matcher": "Bash", "hooks": [{"type": "command", "command": "/home/me/bin/policy"}]}]
    }}

Only that file is read, never a project's `.claude/settings.json`: a hook runs a command,
and a cloned repo must not choose one. LLM_LIGHT_HOOKS_CONFIG moves it (the test suite
points it at a temp file). It is its own file because /config rewrites settings.json with
only the keys it knows.

- Subscription: the hooks go into the claude CLI agent's --settings file, after
  llm_light's own shell guard, and the CLI runs every event as Claude Code would. The
  agent runs with HOME pointed at an empty temp dir, so a command should not rely on `~`.
- API key: llm_light runs PreToolUse and PostToolUse itself, around its tools and after
  it writes a SEARCH/REPLACE edit, under Claude Code's tool names (Read, Grep, Glob, Bash,
  Edit, Write, mcp__...) so one matcher serves both modes. A command gets the event as
  JSON on stdin. Exit 2 blocks a PreToolUse call, and its stderr tells the model why; on
  PostToolUse it goes to the model with the result. Any other failure, a timeout
  included, is noted and the call goes on. JSON output on stdout is not read.
"""

import json
import os
import re
import subprocess
from pathlib import Path

from llm_light import config
from llm_light.ui import notify

EVENTS = ("PreToolUse", "PostToolUse", "Notification", "UserPromptSubmit", "Stop",
          "SubagentStop", "PreCompact", "SessionStart", "SessionEnd")
DEFAULT_TIMEOUT = 60
# llm_light's API-key tools under the names Claude Code's matchers use.
CLI_NAMES = {"read_file": "Read", "grep": "Grep", "list_files": "Glob", "shell": "Bash"}
_STDERR_MAX = 2000


def config_file() -> Path:
    return Path(config.env_get("HOOKS_CONFIG") or Path.home() / ".llm_light" / "hooks.json")


def _group(raw) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("each entry must be an object with \"hooks\"")
    matcher = raw.get("matcher", "")
    if not isinstance(matcher, str):
        raise ValueError("matcher must be a string")
    if matcher not in ("", "*"):
        try:
            re.compile(matcher)
        except re.error as e:
            raise ValueError(f"matcher {matcher!r} is not a regular expression: {e}")
    commands = raw.get("hooks")
    if not isinstance(commands, list) or not commands:
        raise ValueError("needs a non-empty \"hooks\" list")
    out = []
    for h in commands:
        if not isinstance(h, dict) or h.get("type") != "command":
            raise ValueError("a hook must be {\"type\": \"command\", \"command\": ...}")
        if not isinstance(h.get("command"), str) or not h["command"].strip():
            raise ValueError("a hook needs a command")
        timeout = h.get("timeout", DEFAULT_TIMEOUT)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("timeout must be a positive number of seconds")
        out.append({"type": "command", "command": h["command"],
                    **({"timeout": h["timeout"]} if "timeout" in h else {})})
    return {**({"matcher": matcher} if "matcher" in raw else {}), "hooks": out}


def load() -> tuple:
    """(hooks, problems): the valid groups by event, and one line for each event, or the
    whole file, that was skipped."""
    path = config_file()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, []
    except OSError as e:
        return {}, [f"{path}: cannot read it: {e}"]
    try:
        data = json.loads(text)
    except ValueError as e:
        return {}, [f"{path}: not valid JSON: {e}"]
    raw = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return {}, [f"{path}: needs a \"hooks\" object"]
    found, problems = {}, []
    for event, groups in raw.items():
        if event not in EVENTS:
            problems.append(f"{path}: {event} skipped: not one of {', '.join(EVENTS)}")
            continue
        try:
            if not isinstance(groups, list):
                raise ValueError("must be a list")
            found[event] = [_group(g) for g in groups]
        except ValueError as e:
            problems.append(f"{path}: {event} skipped: {e}")
    return found, problems


_loaded = None


def configured() -> dict:
    global _loaded
    if _loaded is None:
        _loaded = load()
    return _loaded[0]


def problems() -> list:
    configured()
    return _loaded[1]


def reset() -> None:
    global _loaded
    _loaded = None


def cli_hooks() -> dict:
    """The hooks for the CLI agent's --settings file."""
    return json.loads(json.dumps(configured()))  # a copy the caller may extend


# --- API-key mode -----------------------------------------------------------------------------

def _matches(matcher, tool_name: str) -> bool:
    if matcher in (None, "", "*"):
        return True
    return re.fullmatch(matcher, tool_name) is not None


def _run_one(hook: dict, payload: dict, cwd: Path) -> tuple:
    """(exit code or None on a timeout or failure to start, stderr text)."""
    from llm_light import user_shell
    from llm_light.providers.runner import kill_process_tree, new_session
    timeout = hook.get("timeout", DEFAULT_TIMEOUT)
    argv, shell = user_shell.command_argv(hook["command"])
    try:
        proc = subprocess.Popen(argv, shell=shell, cwd=str(cwd), stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace",
                                start_new_session=new_session())
    except OSError as e:
        return None, f"could not start: {e}"
    try:
        _, err = proc.communicate(json.dumps(payload), timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_process_tree(proc)
        try:
            proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001  it is being killed; nothing to add
            pass
        return None, f"timed out after {timeout:g}s"
    return proc.returncode, (err or "").strip()[-_STDERR_MAX:]


def run(event: str, tool_name: str, tool_input: dict, cwd: Path, tool_response=None) -> tuple:
    """Run the hooks of `event` whose matcher fits `tool_name`. Returns (blocked, text):
    blocked when one exited 2, with the stderr of those that did."""
    groups = configured().get(event)
    if not groups:
        return False, ""
    payload = {"hook_event_name": event, "tool_name": tool_name, "tool_input": tool_input,
               "cwd": str(cwd)}
    if tool_response is not None:
        payload["tool_response"] = tool_response
    blocked, said = False, []
    for group in groups:
        if not _matches(group.get("matcher"), tool_name):
            continue
        for hook in group["hooks"]:
            code, err = _run_one(hook, payload, cwd)
            if code == 2:
                blocked = True
                said.append(err or f"{hook['command']} exited 2")
            elif code != 0:
                why = f"exit {code}" if code is not None else "failed"
                notify(f"{event} hook {why}: {err or hook['command']}", error=True)
    return blocked, "\n".join(said)


def tool_input_for(name: str, tool_input, root: Path) -> dict:
    """A tool call's input as a hook sees it: Read gets an absolute file_path."""
    data = dict(tool_input) if isinstance(tool_input, dict) else {}
    if name == "read_file" and isinstance(data.get("path"), str):
        data["file_path"] = str((root / data["path"]).resolve())
    return data


def after_edit(path: str, new: bool) -> None:
    """PostToolUse for a file llm_light just wrote from a SEARCH/REPLACE block. No model
    is waiting for a result here, so exit 2's stderr is shown to the user."""
    root = Path.cwd().resolve()
    tool = "Write" if new else "Edit"
    blocked, said = run("PostToolUse", tool, {"file_path": str((root / path).resolve())}, root,
                        tool_response={"filePath": str((root / path).resolve()), "success": True})
    if blocked and said:
        print(f"  PostToolUse hook on {path}: {said}")


# --- /hooks and /doctor -----------------------------------------------------------------------

def command_lines() -> list:
    found = configured()
    if not found:
        return [f"No hooks. Add them to {config_file()}, in the shape of Claude Code's "
                "\"hooks\" setting, and restart."] + list(problems())
    lines = []
    for event, groups in found.items():
        for group in groups:
            matcher = group.get("matcher") or "*"
            for hook in group["hooks"]:
                lines.append(f"{event} [{matcher}]: {hook['command']}")
    return lines + list(problems())
