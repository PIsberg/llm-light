"""The session commands modelled on Claude Code's: /copy, /model, /export,
/status, /diff, `!command` and `@file` mentions.

Each returns the line to show (or prints its own output for the ones that
show a diff or a command's output), so main.py's prompt loop only dispatches.
"""

import base64
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import llm_light.state as state
from llm_light import config, git_manager, permissions, providers
from llm_light.providers.base import SHELL, TOP_TIER


# --- version ------------------------------------------------------------------

def version() -> str:
    """The project version: pyproject.toml in a source checkout (what the
    build stamps), else the installed package's metadata."""
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    try:
        import tomllib
        return tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    except (OSError, ValueError, KeyError):
        pass
    try:
        from importlib.metadata import version as _dist_version
        return _dist_version("llm-light")
    except Exception:
        return "unknown"


# --- /copy --------------------------------------------------------------------

_MAX_REPLIES = 50


def record_reply(reply: str) -> None:
    """Keep a turn's full reply (code blocks included) for /copy."""
    if not reply:
        return
    with state.lock:
        state.replies.append(reply)
        del state.replies[:-_MAX_REPLIES]


def _history_text(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text")


def _clipboard_command() -> list | None:
    """The command that reads stdin into the system clipboard, or None."""
    if sys.platform == "win32":
        return ["clip"]
    if sys.platform == "darwin":
        return ["pbcopy"] if shutil.which("pbcopy") else None
    for name, argv in (("wl-copy", ["wl-copy"]),
                       ("xclip", ["xclip", "-selection", "clipboard"]),
                       ("xsel", ["xsel", "--clipboard", "--input"])):
        if shutil.which(name):
            return argv
    return None


def copy_text(text: str) -> str:
    """Put `text` on the clipboard. Without a clipboard tool (or when it
    fails, as over SSH), send it as an OSC 52 escape, which many terminals
    copy to the local clipboard. Returns the line to show."""
    argv = _clipboard_command()
    if argv:
        # clip.exe reads UTF-16 as Unicode (UTF-8 mangles non-ASCII text), and
        # keeps a BOM as a U+FEFF at the front of the paste: little-endian, no BOM.
        data = text.encode("utf-16-le") if argv[0] == "clip" else text.encode("utf-8")
        try:
            proc = subprocess.run(argv, input=data, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=10)
            if proc.returncode == 0:
                return f"Copied {len(text)} characters to the clipboard."
        except (OSError, subprocess.SubprocessError):
            pass
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    sys.stdout.write(f"\x1b]52;c;{payload}\x07")
    sys.stdout.flush()
    return (f"Sent {len(text)} characters to the terminal clipboard (OSC 52); "
            "not every terminal supports it, so paste to check.")


def copy_reply(arg: str) -> str:
    """`/copy [n]`: copy the latest reply, or the n-th latest."""
    arg = arg.strip()
    if arg and not (arg.isdigit() and int(arg) > 0):
        return "Usage: /copy [n]  (n = 1 for the latest reply, 2 for the one before)"
    n = int(arg) if arg else 1
    with state.lock:
        replies = list(state.replies)
        if not replies:  # e.g. after --continue: fall back to the saved conversation
            replies = [_history_text(m["content"]) for m in state.conversation_history
                       if m.get("role") == "assistant"]
    replies = [r for r in replies if r.strip()]
    if not replies:
        return "Nothing to copy yet."
    if n > len(replies):
        return f"There are only {len(replies)} replies to copy."
    return copy_text(replies[-n])


# --- !command -------------------------------------------------------------------

def run_bang(command: str) -> str:
    """`!command`: run it in the project root and queue its output to go with
    the next question, so the model sees what the user saw. The user typed
    it, so no allowlist applies (as in Claude Code's bash mode)."""
    command = command.strip()
    if not command:
        return "Usage: !<shell command>"
    from llm_light import executor
    transcript = executor._run_command(command)  # prints the output as it goes
    with state.lock:
        state.pending_context.append(
            f"I ran `{command}` in the project root. Its output:\n```\n{transcript}\n```")
    return "The output goes with your next message."


def take_pending() -> str:
    """Queued `!command` output, once; "" when there is none."""
    with state.lock:
        text = "\n\n".join(state.pending_context)
        state.pending_context.clear()
    return text


# --- @file mentions ---------------------------------------------------------------

MENTION_MAX_CHARS = 50_000
# "@path" at the start or after whitespace, so me@example.com is not one.
_MENTION = re.compile(r"(?<!\S)@([^\s@]+)")
_TRAILING = ",;:!?)]}\"'"


def _looks_like_path(token: str) -> bool:
    # @Override or @alice are not paths; a slash or a dot makes one likely.
    return any(c in token for c in "/\\.")


def expand_mentions(text: str, root: Path | None = None) -> tuple:
    """Attach the project files `text` mentions as @path.

    Returns (context, attached, problems): the files' contents to send after
    the question ("" when none), their project-relative paths, and one line
    per path-like mention that was not attached (missing, outside the
    project, a directory, binary). A file outside the project root is never
    read, whatever the path says.
    """
    root = Path(root or Path.cwd()).resolve()
    blocks, attached, problems = [], [], []
    for match in _MENTION.finditer(text):
        token = match.group(1).rstrip(_TRAILING)
        candidates = [token] + ([token.rstrip(".")] if token.endswith(".") else [])
        target = None
        for cand in candidates:
            path = (root / cand).resolve()
            if path.is_file():
                target = path
                break
        if target is None:
            if _looks_like_path(token):
                path = (root / token).resolve()
                reason = ("outside the project" if not path.is_relative_to(root)
                          else "a directory" if path.is_dir() else "not found")
                problems.append(f"@{token}: {reason}")
            continue
        if not target.is_relative_to(root):
            problems.append(f"@{token}: outside the project")
            continue
        rel = target.relative_to(root).as_posix()
        if rel in attached:
            continue
        try:
            raw = target.read_bytes()
        except OSError as e:
            problems.append(f"@{token}: {e.strerror or e}")
            continue
        if b"\0" in raw[:8192]:
            problems.append(f"@{token}: a binary file")
            continue
        body = raw.decode("utf-8", errors="replace")
        if len(body) > MENTION_MAX_CHARS:
            body = (body[:MENTION_MAX_CHARS]
                    + f"\n[... truncated: {len(body) - MENTION_MAX_CHARS:,} more characters]")
        attached.append(rel)
        blocks.append(f'<file path="{rel}">\n{body}\n</file>')
    context = ("Files the user attached:\n\n" + "\n\n".join(blocks)) if blocks else ""
    return context, attached, problems


def mention_completions(word: str, root: Path | None = None) -> list:
    """Paths completing the "@..." being typed: files and directories (with a
    trailing /) under the project root, skipping hidden and build directories."""
    root = Path(root or Path.cwd())
    partial = word[1:].replace("\\", "/")
    folder, _, stem = partial.rpartition("/")
    base = root / folder if folder else root
    try:
        entries = sorted(os.scandir(base), key=lambda e: e.name.lower())
    except OSError:
        return []
    out = []
    for entry in entries:
        name = entry.name
        if not name.lower().startswith(stem.lower()):
            continue
        if name.startswith(".") and not stem.startswith("."):
            continue
        is_dir = entry.is_dir()
        if is_dir and name in config.SKIP_DIRS:
            continue
        prefix = f"{folder}/" if folder else ""
        out.append("@" + prefix + name + ("/" if is_dir else ""))
        if len(out) >= 50:
            break
    return out


# --- /model ----------------------------------------------------------------------

MODEL_ALIASES = {
    "opus": config.MODEL_OPUS,
    "sonnet": config.MODEL_SONNET,
    "haiku": config.MODEL_HAIKU,
    "fable": config.MODEL_FABLE,
}


def resolve_model(name: str) -> str | None:
    """A model id from an alias (opus, sonnet, haiku, fable) or a priced id."""
    name = (name or "").strip().lower()
    if name in MODEL_ALIASES:
        return MODEL_ALIASES[name]
    return name if name in config.MODEL_PRICES else None


def _short(model: str) -> str:
    return model.replace("claude-", "")


def model_command(arg: str) -> str:
    """`/model [name|auto]`: show, pin or unpin the model."""
    provider = providers.active()
    arg = arg.strip()
    if not provider.has(TOP_TIER):
        return f"/model unavailable for {provider.name}: its own tool picks the model."
    if not arg:
        if state.pinned_model:
            return (f"Model: {_short(state.pinned_model)} (pinned). "
                    "/model auto returns to routing per question.")
        current = f"; this session: {_short(state.session_model)}" if state.session_model else ""
        return (f"Model: routed per question{current}. "
                f"/model {'|'.join(MODEL_ALIASES)} pins one.")
    if arg.lower() in ("auto", "default", "off"):
        state.pinned_model = None
        return "Model: automatic routing again."
    model = resolve_model(arg)
    if model is None:
        return f"Unknown model {arg!r}. Choose {', '.join(MODEL_ALIASES)}, or auto."
    state.pinned_model = model
    return f"Model pinned to {_short(model)}: every question runs on it until /model auto."


# --- /export ----------------------------------------------------------------------

def export_conversation(arg: str) -> str:
    """`/export [file]`: write the conversation as plain text. The default
    name is a .txt in the project: a .md would be picked up by the skeleton
    and change the cached prompt."""
    with state.lock:
        history = list(state.conversation_history)
    if not history:
        return "Nothing to export yet."
    now = datetime.now()
    path = Path(arg.strip()).expanduser() if arg.strip() else \
        Path.cwd() / f"llm_light-conversation-{now:%Y%m%d-%H%M%S}.txt"
    lines = [f"llm_light conversation in {Path.cwd()}, exported {now:%Y-%m-%d %H:%M}", ""]
    for message in history:
        role = "You" if message.get("role") == "user" else "Assistant"
        lines += [f"## {role}", "", _history_text(message.get("content", "")).strip(), ""]
    try:
        path.write_text("\n".join(lines), encoding="utf-8")
    except OSError as e:
        return f"Could not export: {e}"
    return f"Exported {len(history)} messages to {path}."


# --- /status ------------------------------------------------------------------------

def status_lines() -> list:
    """`/status`: version, provider, model, permission mode, shell, index."""
    from llm_light import session
    from llm_light.ui import access_label
    provider = providers.active()
    header = provider.header()
    access = header[1] if isinstance(header, tuple) and len(header) > 1 else \
        access_label(config.API_KEY_SOURCE)

    if state.pinned_model:
        model = f"{_short(state.pinned_model)} (pinned with /model)"
    elif not provider.has(TOP_TIER):
        model = f"chosen by {provider.name}"
    elif state.session_model:
        model = f"routed per question; this session: {_short(state.session_model)}"
    else:
        model = "routed per question"

    mode = permissions.mode()
    if not provider.has(SHELL):
        shell = f"unavailable for {provider.name}"
    elif permissions.plan():
        shell = "off (plan mode)"
    elif permissions.bypass():
        shell = "any command (permissions bypassed)"
    elif config.ALLOWED_COMMANDS:
        shell = "allowed: " + "; ".join(config.ALLOWED_COMMANDS)
    else:
        shell = "off (--allow-command enables a command)"

    index = (f"{len(state.chunk_store):,} chunks" if state.indexing_done.is_set()
             else "building")
    turns = state.turn_count
    return [
        f"llm_light {version()}",
        f"Project       {Path.cwd()}",
        f"Provider      {provider.name}" + (f"  ·  {access}" if access else ""),
        f"Model         {model}",
        f"Permissions   {mode}: {permissions.DESCRIPTIONS[mode]}",
        f"Shell         {shell}",
        f"Index         {index}",
        f"Conversation  {turns} turn{'' if turns == 1 else 's'}, saved to {session.session_file()}",
    ]


# --- /diff ---------------------------------------------------------------------------

def diff_command() -> str:
    """`/diff`: show the uncommitted changes under the current directory, file
    by file, and name the untracked files. Returns a summary line."""
    if not git_manager.is_git_repo():
        return "Not in a git repository."
    names, code = git_manager._run_git("diff", "HEAD", "--name-only", "--relative")
    base = ["HEAD"]
    if code != 0:  # no commit yet: compare with the index instead
        names, code = git_manager._run_git("diff", "--name-only", "--relative")
        base = []
    untracked, _ = git_manager._run_git("ls-files", "--others", "--exclude-standard")
    files = [n for n in names.splitlines() if n.strip()]
    new = [n for n in untracked.splitlines() if n.strip()]
    if not files and not new:
        return "No uncommitted changes."
    from llm_light import ui
    for name in files:
        out, _ = git_manager._run_git("diff", *base, "--relative", "--", name)
        ui.show_unified_diff(name, out)
    summary = f"{len(files)} changed file{'' if len(files) == 1 else 's'}"
    if new:
        more = f" and {len(new) - 10} more" if len(new) > 10 else ""
        summary += f"; untracked: {', '.join(new[:10])}{more}"
    return summary + "."
