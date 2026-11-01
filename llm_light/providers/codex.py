"""Codex provider: the `codex` CLI as a subprocess."""
import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

from llm_light.providers.base import USAGE, Provider, ProviderError, json_events, transcript
from llm_light.providers.runner import empty_temp_dir, run_cli

BINARY = "codex"
_LABEL = "CLI default model"
_ARGV_TAIL = [
    "exec", "--json", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config",
    "--sandbox", "read-only",
    "--disable", "shell_tool", "--disable", "unified_exec", "--disable", "apps", "-",
]

# codex's base instructions, written to a file and named by -c model_instructions_file.
# llm_light's own instructions travel in the message, so codex's ~18,000 characters of
# agent instructions only cost tokens. Without "no tools", codex took extra hidden model
# rounds on 4 of 8 runs of one question, each re-sending the whole prompt (docs/BENCHMARKS.md).
INSTRUCTIONS = ("You are a code assistant. You have no tools in this session: do not call "
                "any tool, run any command or read any file. Everything you can use is in "
                "the user's message, which begins with the instructions to follow. Answer "
                "in a single reply.\n")
# Sections of codex's own prompt an answer engine does not use, and the tool schemas of
# features it never calls. Each goes in as `-c`: codex ignores an unknown config key, but
# exits 1 on `--disable <unknown feature>`, so a renamed feature cannot break every call.
_TRIM_CONFIG = (
    "skills.include_instructions=false",
    "include_permissions_instructions=false",
    "include_collaboration_mode_instructions=false",
    "include_environment_context=false",
) + tuple(f"features.{name}=false" for name in (
    "multi_agent", "image_generation", "browser_use", "computer_use", "goals", "plugins",
    "skill_search", "tool_suggest", "view_image", "sleep_tool", "in_app_browser",
    "workspace_dependencies",
))


def _argv_tail(instructions_path: str) -> list:
    """_ARGV_TAIL with the token-trim `-c` flags inserted before the stdin `-`."""
    config = [f"model_instructions_file={json.dumps(instructions_path)}", *_TRIM_CONFIG]
    flags = [flag for value in config for flag in ("-c", value)]
    return _ARGV_TAIL[:-1] + flags + _ARGV_TAIL[-1:]


# An unreadable reply most likely means a codex release changed its JSON events.
_PARSE_REMEDY = ("Update codex (npm install -g @openai/codex) and run it on its own to see "
                 "what it prints.")


def _agent_reply(stdout: str) -> str:
    """The text of the last agent_message event in codex's JSONL output."""
    reply = None
    for event in json_events(stdout):
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            reply = item.get("text")
    if not isinstance(reply, str) or not reply.strip():
        raise ProviderError("codex output could not be parsed: no agent_message in the output. "
                            + _PARSE_REMEDY)
    return reply.strip()


def _count(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(value)
    return value


def _usage(stdout: str):
    """Token counts from codex's turn.completed events, summed; None if unreadable.

    codex counts cached tokens inside input_tokens. Claude's usage keeps them
    apart, and the session totals add the two, so the cached part is split out.
    """
    total = None
    for event in json_events(stdout):
        if event.get("type") != "turn.completed":
            continue
        usage = event.get("usage")
        try:
            if not isinstance(usage, dict):
                raise ValueError(usage)
            cached = _count(usage.get("cached_input_tokens", 0))
            row = (max(0, _count(usage["input_tokens"]) - cached), cached,
                   _count(usage["output_tokens"]))
        except (KeyError, ValueError):
            return None
        total = row if total is None else tuple(a + b for a, b in zip(total, row))
    if total is None:
        return None
    return SimpleNamespace(input_tokens=total[0], cache_read_input_tokens=total[1],
                           cache_creation_input_tokens=0, output_tokens=total[2])


class CodexProvider(Provider):
    name = "codex"
    # Exact token counts, at prices that are not Claude's: no USD_BUDGET (#148).
    capabilities = frozenset({USAGE})

    def model_label(self, routed_model: str) -> str:
        return _LABEL

    def header(self):
        return (_LABEL, "codex CLI")

    def require_auth(self) -> None:
        from llm_light.ui import _T_ERR
        if not shutil.which(BINARY):
            print(f"\n  {_T_ERR}  codex CLI not found. Install it: "
                  f"npm install -g @openai/codex", file=sys.stderr)
            sys.exit(1)
        home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        if (home / "auth.json").exists():
            return
        print(f"\n  {_T_ERR}  codex is not logged in. Run: codex login", file=sys.stderr)
        sys.exit(1)

    def _run(self, stdin_text: str) -> tuple:
        """(reply, usage) of one codex run; usage is None when codex printed none."""
        binary = shutil.which(BINARY)
        if not binary:
            raise ProviderError("codex CLI not found. Install it: "
                                "npm install -g @openai/codex")
        # The instructions file has its own directory: the CLI's cwd stays empty.
        with empty_temp_dir() as cwd, empty_temp_dir() as config_dir:
            instructions = os.path.join(config_dir, "instructions.md")
            Path(instructions).write_text(INSTRUCTIONS, encoding="utf-8")
            out = run_cli(self.name, [binary] + _argv_tail(instructions), stdin_text, cwd=cwd)
        return _agent_reply(out), _usage(out)

    def complete(self, request: dict):
        from llm_light.ui import _Spinner
        system_text, prompt = transcript(request)
        with _Spinner("Waiting for codex"):
            reply, usage = self._run(system_text + "\n\n" + prompt)
        return reply, usage, False

    def summarize(self, instructions: str):
        from llm_light.ui import _Spinner
        with _Spinner("Summarizing"):
            return self._run("You summarize conversations.\n\n" + instructions)


PROVIDER = CodexProvider()
