"""Gemini provider: Google's Antigravity CLI (`agy`) as a subprocess."""
import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

from llm_light import proxy
from llm_light.providers import base
from llm_light.providers.base import USAGE, Provider, ProviderError, json_events, transcript
from llm_light.providers.runner import empty_temp_dir, run_cli

BINARY = "agy"
_LABEL = "CLI default model"
_MISSING = "agy not found. Install the Antigravity CLI: https://antigravity.google"
# Deny shell, MCP and file tools: agy otherwise runs the user's globally allowed MCP shell.
SETTINGS = json.dumps({
    "permissions": {"allow": [], "deny": ["command", "mcp", "file"]},
    "allowNonWorkspaceAccess": False,
})
# agy's own settings file; only its `model` is read (#118).
_USER_SETTINGS = (".gemini", "antigravity-cli", "settings.json")
# An unreadable reply most likely means an agy release changed its stream-json events.
_PARSE_REMEDY = "Update agy (agy update) and run it on its own to see what it prints."
# Where agy keeps per-conversation state, its plan artifacts included (#123).
_BRAIN = (".gemini", "antigravity-cli", "brain")
# What _carry_sign_in() brings into the run's temporary home (#124).
_TOKEN_FILE = (".gemini", "antigravity-cli", "antigravity-oauth-token")
_KEYCHAINS = ("Library", "Keychains")

# A custom agent with no tools, selected with --agent. No permission kind stops
# agy's search_web, so a Gemini turn could put repo code into web-search queries
# (#122). excludeDefaultComponents drops agy's built-in tools and its own prompt
# sections; probed with agy 1.4.12 on a prompt that asks for a web search, the
# default agent ran search_web 3 times at 34,648 input tokens, this one ran no
# tool in 5 of 5 runs at 1,249 input tokens (#149). Written into the run's own
# temporary home, which agy reads custom agents from, so the cwd stays empty.
AGENT_NAME = "llm-light"
_AGENT_FILE = (".gemini", "config", "agents", f"{AGENT_NAME}.md")
_AGENT = f"""---
name: {AGENT_NAME}
description: Answers questions about code from the text in the message.
excludeDefaultComponents: true
tools: []
---
You answer questions about a codebase using only the text in the user's message. You have no
tools. For an edit, write SEARCH/REPLACE blocks from the code shown in the message.
"""


def _argv_tail(settings: str) -> list:
    # The empty attached `-p=` is required: a bare `-p` swallows the next flag as its prompt.
    return ["--input-format", "stream-json", "--output-format", "stream-json",
            "--mode", "plan", "--agent", AGENT_NAME, "--settings", settings, "-p="]


def _install_agent(home: str) -> None:
    path = Path(home).joinpath(*_AGENT_FILE)
    path.parent.mkdir(parents=True)
    path.write_text(_AGENT, encoding="utf-8")


def _settings() -> str:
    """SETTINGS plus the user's chosen agy model, if their settings name one.

    agy runs with an empty home (_isolated_env), so it would otherwise fall
    back to its default model rather than the one the user picked.
    """
    try:
        user = json.loads(Path.home().joinpath(*_USER_SETTINGS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return SETTINGS
    model = user.get("model") if isinstance(user, dict) else None
    if not isinstance(model, str) or not model:
        return SETTINGS
    return json.dumps(dict(json.loads(SETTINGS), model=model))


def _carry_sign_in(home: str) -> None:
    """Make the user's agy sign-in reachable from the run's temporary home, and nothing else.

    Windows needs nothing: agy uses the Credential Manager, which ignores HOME. Elsewhere
    a redirected HOME loses it, so agy would ask a signed-in user to sign in (#124):
    - Without a usable OS keychain agy reads a token file under HOME (strace of agy 1.4.12
      on Linux). The copy goes away with the home; a token agy refreshes during the run
      is not written back, and the user's own stays as it was.
    - macOS: agy calls /usr/bin/security, which finds no login keychain once HOME moves
      (exit 44, macOS 26.6.2) and finds it with only Library/Keychains linked. rmtree
      removes the link without following it.
    """
    real = Path.home()
    token = real.joinpath(*_TOKEN_FILE)
    try:
        if token.is_file():
            target = Path(home).joinpath(*_TOKEN_FILE)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(token, target)
            target.chmod(0o600)
    except OSError:
        pass  # agy then says for itself that it is not signed in
    keychains = real.joinpath(*_KEYCHAINS)
    if sys.platform == "darwin" and keychains.is_dir():
        link = Path(home).joinpath(*_KEYCHAINS)
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(keychains, target_is_directory=True)


def _isolated_env(home: str) -> dict:
    """Our environment with HOME and USERPROFILE pointing at an empty directory.

    agy then loads none of the user's plugins, hooks, rules or MCP servers
    (all under ~/.gemini) and keeps its conversations and logs out of the
    user's history. _carry_sign_in() keeps the sign-in working there.
    """
    env = proxy.child_env(dict(os.environ))
    env["HOME"] = home
    env["USERPROFILE"] = home
    return env


# Without it agy calls run_command (3 of 3 real runs), is denied, and returns an empty response.
_PREAMBLE = ("You have no tools in this session: do not call any tool, run any command or read "
             "any file. Everything you can use is in this message. For an edit, write "
             "SEARCH/REPLACE blocks from the code shown here.\n\n")
# Tools the deny settings forbid, and the denied_actions kind that covers each.
_FORBIDDEN = {
    "run_command": "command", "send_command_input": "command", "call_mcp_tool": "mcp",
    "write_to_file": "file", "replace_file_content": "file",
    "multi_replace_file_content": "file", "sed_file": "file", "notebook_edit": "file",
}
# Read-only tools that may finish without a denial. The llm-light agent has none of
# them; they stay allowed because they cannot reach the project (empty cwd,
# allowNonWorkspaceAccess false). search_web is not here: a search means the
# agent did not load, and the query may quote repo code, so it fails closed (#122).
_ALLOWED_TOOLS = frozenset({"read_file", "view_file", "list_dir", "find_by_name", "grep_search"})


def _stdin_line(text: str) -> str:
    content = _PREAMBLE + text
    return json.dumps({"event": "user", "message": {"role": "user", "content": content}}) + "\n"


def _is_plan_artifact(step: dict, home) -> bool:
    """A write_file step whose Path is inside this run's own agy brain directory.

    In plan mode agy saves a plan there (<home>/.gemini/antigravity-cli/brain/<id>/plan.md,
    agy 1.4.12, #123). home is the run's temporary home, removed afterwards.
    """
    if step.get("tool_name") != "write_file" or not home:
        return False
    info = step.get("tool_info")
    params = info.get("parameters") if isinstance(info, dict) else None
    path = params.get("Path") if isinstance(params, dict) else None
    if not isinstance(path, str) or not os.path.isabs(path):
        return False
    brain = os.path.normcase(os.path.realpath(os.path.join(home, *_BRAIN)))
    target = os.path.normcase(os.path.realpath(path))
    try:
        return os.path.commonpath([brain, target]) == brain and target != brain
    except ValueError:  # different drives on Windows
        return False


def _check_tools(stdout: str, result: dict, home=None) -> None:
    """Fail closed: a finished tool call passes only if it is read-only, or agy denied it.

    A tool outside the allowlist and the `_FORBIDDEN` kinds raises, except agy's plan artifact
    in the run's own home (`_is_plan_artifact`). A forbidden tool passes only while the DONE
    steps of its kind do not outnumber the `denied_actions` records of that kind.
    """
    denied = result.get("denied_actions")
    denials: dict = {}
    for a in denied if isinstance(denied, list) else []:
        if isinstance(a, dict):
            denials[a.get("action")] = denials.get(a.get("action"), 0) + 1
    done: dict = {}
    for event in json_events(stdout):
        step = event.get("step_update") if event.get("event") == "step_update" else None
        if (not isinstance(step, dict) or step.get("step_type") != "tool"
                or step.get("state") != "DONE"):
            continue
        tool = step.get("tool_name")
        if tool in _ALLOWED_TOOLS or _is_plan_artifact(step, home):
            continue
        kind = _FORBIDDEN.get(tool)
        if kind:
            done[kind] = done.get(kind, 0) + 1
        if not kind or done[kind] > denials.get(kind, 0):
            raise ProviderError(f"agy ran {tool}, which llm_light forbids. Ask again; if it "
                                "keeps happening, choose another --provider.")


def _result_reply(stdout: str, home=None) -> str:
    """The response of the last `result` event; ProviderError if it is missing or failed.

    home is the run's temporary home, where agy may write its plan artifact.
    """
    result = None
    for event in json_events(stdout):
        if event.get("event") == "result" and isinstance(event.get("result"), dict):
            result = event["result"]
    if result is None:
        raise ProviderError("agy output could not be parsed: no result event in the output. "
                            + _PARSE_REMEDY)
    if result.get("status") != "SUCCESS":
        raise ProviderError(f"agy failed: {result.get('error') or result.get('status')}. "
                            f"If you are not signed in, run agy once and sign in.")
    _check_tools(stdout, result, home)
    reply = result.get("response")
    if not isinstance(reply, str) or not reply.strip():
        denied = result.get("denied_actions")
        if isinstance(denied, list) and denied and isinstance(denied[0], dict):
            name = denied[0].get("display_name") or denied[0].get("action")
            raise ProviderError(f"agy tried {name} and returned nothing: llm_light denies "
                                f"agy's tools, so the request cannot be answered.")
        raise ProviderError("agy output could not be parsed: empty response. " + _PARSE_REMEDY)
    return reply.strip()


def _count(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(value)
    return value


def _usage(stdout: str):
    """Token counts from the last result event's usage; None if unreadable (#159).

    agy's input_tokens excludes the cached part, which it reports as
    cache_read_tokens, the split Claude's usage already has. output_tokens
    includes the thinking tokens.
    """
    usage = None
    for event in json_events(stdout):
        if event.get("event") == "result" and isinstance(event.get("result"), dict):
            usage = event["result"].get("usage")
    try:
        if not isinstance(usage, dict):
            raise ValueError(usage)
        return SimpleNamespace(input_tokens=_count(usage["input_tokens"]),
                               cache_read_input_tokens=_count(usage.get("cache_read_tokens", 0)),
                               cache_creation_input_tokens=0,
                               output_tokens=_count(usage["output_tokens"]))
    except (KeyError, ValueError):
        return None


class GeminiProvider(Provider):
    name = "gemini"
    # Exact token counts, at prices that are not Claude's: no USD_BUDGET (#159).
    capabilities = frozenset({USAGE})

    def model_label(self, routed_model: str) -> str:
        return _LABEL

    def header(self):
        return (_LABEL, "agy CLI")

    def diagnose(self) -> list:
        path = shutil.which(BINARY)
        if not path:
            return [(base.PROBLEM, _MISSING)]
        return [(base.OK, f"agy: {path}"),
                (base.NOTE, "agy sign-in: not checked; a missing one shows on the first question")]

    def require_auth(self) -> None:
        from llm_light.ui import _T_ERR
        if not shutil.which(BINARY):
            print(f"\n  {_T_ERR}  {_MISSING}", file=sys.stderr)
            sys.exit(1)

    def _run(self, stdin_text: str) -> tuple:
        """(reply, usage) of one agy run; usage is None when agy printed none."""
        binary = shutil.which(BINARY)
        if not binary:
            raise ProviderError(_MISSING)
        argv = [binary] + _argv_tail(_settings())
        with empty_temp_dir() as cwd, empty_temp_dir() as home:
            _install_agent(home)
            _carry_sign_in(home)
            out = run_cli(self.name, argv, _stdin_line(stdin_text), cwd=cwd,
                          env=_isolated_env(home))
            return _result_reply(out, home), _usage(out)

    def complete(self, request: dict):
        from llm_light.ui import _Spinner
        system_text, prompt = transcript(request)
        with _Spinner("Waiting for gemini"):
            reply, usage = self._run(system_text + "\n\n" + prompt)
        return reply, usage, False

    def summarize(self, instructions: str):
        from llm_light.ui import _Spinner
        with _Spinner("Summarizing"):
            return self._run("You summarize conversations.\n\n" + instructions)


PROVIDER = GeminiProvider()
