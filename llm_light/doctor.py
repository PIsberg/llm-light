"""/doctor and --doctor: is this install healthy? And /context: what does a turn carry?

/doctor runs before the login check, since a broken login is the likeliest reason to run
it, and only looks: it starts no program and sends nothing. Provider checks live in
Provider.diagnose(), so neutral code names no provider binary.
"""

import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

import llm_light.state as state
from llm_light import commands, config, permissions, providers, proxy, user_shell
from llm_light.providers.base import AGENT, NOTE, OK, PROBLEM, SHELL

# The context window every routed model has, for the percentage in /context.
CONTEXT_WINDOW = 200_000

_MARKS = {OK: "ok", NOTE: "--", PROBLEM: "!!"}


def _python() -> tuple:
    version = sys.version_info
    label = f"Python {version[0]}.{version[1]}.{version[2]}"
    if tuple(version[:2]) < (3, 12):
        return PROBLEM, f"{label}: llm_light needs 3.12 or newer"
    return OK, label


def _in_repository() -> bool:
    """Whether a parent holds .git, found by looking: /doctor runs no git."""
    here = Path.cwd().resolve()
    return any((folder / ".git").exists() for folder in (here, *here.parents))


def _git() -> tuple:
    if not shutil.which("git"):
        return PROBLEM, "git not found: /undo, /rewind, /diff and auto-commit need it"
    if not _in_repository():
        return NOTE, "git: this folder is not a repository, so /undo and /rewind cannot restore files"
    return OK, "git: in a repository"


def _writable(label: str, folder: Path) -> tuple:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".llm_light_doctor"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        return PROBLEM, f"{label}: cannot write to {folder}: {e}"
    return OK, f"{label}: {folder}"


def _optional(module: str, label: str, without: str) -> tuple:
    if importlib.util.find_spec(module) is None:
        return NOTE, f"{label}: not installed; {without}"
    return OK, f"{label}: installed"


def _mcp(provider) -> tuple:
    """Reads ~/.llm_light/mcp.json; starts no server."""
    from llm_light import mcp_servers
    from llm_light.providers.base import MCP
    servers, problems = mcp_servers.load()
    if problems:
        more = f" ({len(problems) - 1} more)" if len(problems) > 1 else ""
        return PROBLEM, f"MCP servers: {problems[0]}{more}"
    if not servers:
        return NOTE, f"MCP servers: none; add them to {mcp_servers.config_file()}"
    names = ", ".join(servers)
    if not provider.has(MCP):
        return NOTE, f"MCP servers: {names}; not used, since {provider.name} runs without tools"
    if config.AUTH_MODE == "API_KEY" and importlib.util.find_spec("mcp") is None:
        return PROBLEM, f"MCP servers: {names}; the mcp package is not installed: {mcp_servers.INSTALL_HINT}"
    return OK, f"MCP servers: {names}"


def _index() -> tuple:
    from llm_light.config import INDEXABLE_EXTENSIONS
    cache = config.CACHE_DIR
    size = sum(p.stat().st_size for p in cache.rglob("*") if p.is_file()) if cache.is_dir() else 0
    where = f"{size / 1_048_576:.1f} MB cached in {cache}" if size else "nothing cached yet"
    return OK, f"Index: {len(INDEXABLE_EXTENSIONS)} file types, {where}"


def _terminal() -> tuple:
    encoding = (getattr(sys.stdout, "encoding", "") or "unknown").lower()
    columns = shutil.get_terminal_size().columns
    tty = sys.stdout.isatty()
    detail = f"Terminal: {'yes' if tty else 'not a terminal'}, {columns} columns, encoding {encoding}"
    if os.environ.get("NO_COLOR"):
        detail += ", NO_COLOR set"
    if tty and "utf" not in encoding:
        return NOTE, detail + "; symbols fall back to ASCII"
    return OK, detail


def _clipboard() -> tuple:
    if commands._clipboard_command():
        return OK, "Clipboard: /copy has a clipboard tool"
    return NOTE, "Clipboard: no tool found; /copy sends OSC 52, which not every terminal supports"


def checks() -> list:
    """(status, line) for each check."""
    found = [(OK, f"llm_light {commands.version()}"), _python()]
    provider = providers.active()
    found.append((OK, f"Provider: {provider.name}"))
    found.extend(provider.diagnose())
    found.append(_git())
    found.append(_index())
    found.append(_writable("Conversations", Path(config.SESSION_DIR)))
    found.append(_optional("sentence_transformers", "Embeddings", "retrieval falls back to the fast model"))
    found.append(_optional("llmlingua", "LLMLingua", "retrieved code is sent uncompressed"))
    found.append((OK, f"Command shell: {user_shell.name()}"))
    if provider.has(SHELL):
        allowed = config.ALLOWED_COMMANDS
        found.append((OK if allowed else NOTE,
                      "Agent commands: " + ("; ".join(allowed) if allowed else "none; /allow <prefix> adds one")))
    found.append(_mcp(provider))
    found.append((OK, f"Permission mode: {permissions.mode()}"))
    if proxy.active():
        found.append((OK, f"Proxy: {proxy.display()}"))
    found.append(_clipboard())
    found.append(_terminal())
    return found


def run() -> int:
    """Print the report. Returns the exit code for --doctor: 1 if anything is broken."""
    report = checks()
    print()
    for status, line in report:
        print(f"  {_MARKS[status]}  {line}")
    problems = sum(1 for status, _ in report if status == PROBLEM)
    print(f"\n  {problems} problem{'' if problems == 1 else 's'} found." if problems else "\n  No problems found.")
    return 1 if problems else 0


# --- /context ------------------------------------------------------------------------

def _tokens(text: str) -> int:
    return len(text) // 4


def _sends_own_tools() -> bool:
    """API-key turns carry llm_light's tool schemas; the CLI agent has its own."""
    return config.AUTH_MODE == "API_KEY" and providers.active().name == providers.DEFAULT


def _agent_adds_its_own() -> bool:
    return config.AUTH_MODE == "OAUTH" and providers.active().has(AGENT)


def context_rows() -> list:
    """(what, estimated tokens) for what the next turn would send, before retrieval."""
    from llm_light.tools import all_tools
    with state.lock:
        skeleton = state.skeleton_context or ""
        history = list(state.conversation_history)
        pending = list(state.pending_context)
    rows = [("Skeleton", _tokens(config.SYSTEM_PROMPT + skeleton))]
    if _sends_own_tools():
        rows.append(("Tool definitions", _tokens(json.dumps(all_tools()))))
    rows.append(("Conversation", sum(_tokens(commands._history_text(m.get("content", ""))) for m in history)))
    if pending:
        rows.append(("Queued output", sum(_tokens(p) for p in pending)))
    return rows


def print_context() -> None:
    rows = context_rows()
    total = sum(tokens for _, tokens in rows)
    print()
    for label, tokens in rows:
        print(f"  {label:<18}{tokens:>10,}")
    print(f"  {'Total':<18}{total:>10,}  ({total / CONTEXT_WINDOW:.0%} of {CONTEXT_WINDOW:,})")
    notes = "Estimates at 4 characters per token, before this turn's retrieval and question."
    if _agent_adds_its_own():
        notes += " The agent adds its own prompt, tools and whatever it reads."
    print(f"\n  {notes}")
    if total > CONTEXT_WINDOW // 2:
        print("  /compact <focus> replaces the conversation with a summary.")
