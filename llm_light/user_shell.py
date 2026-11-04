"""Which shell runs a command line: `!command`, `/run` and the API-mode `shell` tool.

On Windows, `shell=True` means cmd.exe, while the subscription agent's Bash tool
runs Git Bash, so one line could work for the agent and fail when typed. Git Bash
is used when it is installed, as Claude Code does; LLM_LIGHT_SHELL=cmd keeps
cmd.exe, and LLM_LIGHT_SHELL=<path> names another bash.
"""

import functools
import os
from pathlib import Path

from llm_light.config import env_get


def _on_windows() -> bool:
    return os.name == "nt"


def _existing(path) -> str | None:
    return str(path) if path and Path(path).is_file() else None


@functools.lru_cache(maxsize=1)
def git_bash() -> str | None:
    """Git for Windows' bash.exe, or None.

    Never `bash` on PATH: System32/bash.exe is WSL, with another filesystem and
    usually no python or project tools. Git for Windows' install locations come
    first, then absolute PATH entries outside the project, and only the layouts
    it uses itself.
    """
    for variable, tail in (("ProgramW6432", ("Git",)), ("ProgramFiles", ("Git",)),
                           ("ProgramFiles(x86)", ("Git",)), ("LocalAppData", ("Programs", "Git"))):
        base = os.environ.get(variable)
        found = _existing(Path(base, *tail, "bin", "bash.exe")) if base else None
        if found:
            return found
    cwd = Path.cwd().resolve()
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        folder = Path(entry) if entry else None
        if folder is None or not folder.is_absolute() or not _existing(folder / "git.exe"):
            continue
        folder = folder.resolve()
        if folder.is_relative_to(cwd):
            continue
        root = None
        if folder.name.lower() in ("cmd", "bin"):
            root = folder.parent  # <root>\cmd\git.exe, <root>\bin\git.exe
        if folder.name.lower() == "bin" and folder.parent.name.lower().startswith("mingw"):
            root = folder.parent.parent  # <root>\mingw64\bin\git.exe
        found = _existing(root / "bin" / "bash.exe") if root else None
        if found:
            return found
    return None


def _bash() -> str | None:
    """The bash to use on Windows, or None for cmd.exe."""
    override = env_get("SHELL", "").strip()
    if override.lower() in ("cmd", "cmd.exe"):
        return None
    return _existing(override) if override and _existing(override) else git_bash()


def command_argv(command: str) -> tuple:
    """(args, shell) to give subprocess for one command line."""
    if _on_windows():
        bash = _bash()
        if bash:
            return [bash, "-c", command], False
    return command, True


def name() -> str:
    """The shell's name, for /status."""
    if not _on_windows():
        return "sh"
    bash = _bash()
    if bash is None:
        return "cmd.exe"
    return "Git Bash" if bash == git_bash() else bash
