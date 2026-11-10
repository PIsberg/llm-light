"""The user's own instructions for every project: ~/.llm_light/instructions.md.

They go into the system text after llm_light's own prompt, so they reach the API, the
claude CLI agent, Gemini and Codex alike. ~/.claude/CLAUDE.md stays hidden from the agent:
it is written for Claude Code and names tools llm_light does not have. Read from the home
directory only (LLM_LIGHT_INSTRUCTIONS moves it): a file a cloned repo controls must not
add instructions. Without the file, or with an empty one, requests are unchanged.
"""

from pathlib import Path

from llm_light import config

# Sent with every question, so a long file costs on every turn; past this it is cut.
MAX_CHARS = 8_000


def path() -> Path:
    return Path(config.env_get("INSTRUCTIONS") or Path.home() / ".llm_light" / "instructions.md")


def _raw() -> str:
    try:
        return path().read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def text() -> str:
    """The instructions as they are sent, or "" when there are none. Read on each
    request, so an edit applies from the next turn."""
    raw = _raw()
    if not raw:
        return ""
    if len(raw) > MAX_CHARS:
        raw = raw[:MAX_CHARS] + f"\n[... cut: the file is over {MAX_CHARS:,} characters]"
    return "The user's own instructions, for every project:\n" + raw


def startup_line() -> str | None:
    raw = _raw()
    if not raw:
        return None
    lines = len(raw.splitlines())
    cut = f", cut at {MAX_CHARS:,} characters" if len(raw) > MAX_CHARS else ""
    return (f"Your instructions in {path()} go with every question "
            f"({lines} line{'' if lines == 1 else 's'}{cut}). /memory user edits them.")
