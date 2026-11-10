"""Save the conversation after each turn, so --continue can resume it.

One file per project and provider, named by a hash of the project's absolute path, under
config.SESSION_DIR in the user's home rather than the project's
.llm_light_cache: a cloned repo could otherwise ship a crafted
conversation that --continue would hand to the model as the user's own.

/clear moves that file aside as `<name>.saved-<stamp>.json` instead of
deleting it, keeping the newest config.SESSION_KEEP, and /resume (or
--resume) swaps one of them back in (#136).

/rename gives the conversation a name, saved in the file; --resume <name> finds it
without asking, and a named conversation is never pruned (#210).
"""

import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import llm_light.config as config
import llm_light.state as state
from llm_light import providers

_ROLES = {"user", "assistant"}
NAME_MAX = 60


def session_file(root: Path | None = None, provider: str | None = None) -> Path:
    """This project's file for `provider` (default: the active one). The default
    provider keeps `<digest>.json`; the others get `<digest>.<provider>.json`."""
    provider = provider or providers.active().name
    project = os.path.normcase(str(Path(root or Path.cwd()).resolve()))
    digest = hashlib.sha256(project.encode("utf-8")).hexdigest()[:16]
    suffix = "" if provider == providers.DEFAULT else f".{provider}"
    return Path(config.SESSION_DIR) / f"{digest}{suffix}.json"


def allowed_file(root: Path | None = None) -> Path:
    """Where /allow remembers this project's command prefixes. Outside the project for
    the reason above: a file in a cloned repo must not be able to grant its commands.
    One per project, whichever provider is active."""
    return session_file(root, providers.DEFAULT).with_suffix(".allowed.json")


def save() -> bool:
    """Write the conversation, or remove the file when there is none.
    Returns False, after a warning, if the write failed."""
    path = session_file()
    with state.lock:
        history = list(state.conversation_history)
        data = {
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "project": str(Path.cwd().resolve()),
            "turn_count": state.turn_count,
            "session_model": state.session_model,
            "conversation_history": history,
        }
        if state.session_name:
            data["name"] = state.session_name
    try:
        if not history:
            path.unlink(missing_ok=True)
            return True
        # Conversations quote the user's code: owner-only on POSIX, whatever
        # the umask (Windows ignores these modes). An existing directory is
        # left alone, since LLM_LIGHT_SESSION_DIR may name a shared one.
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.unlink(missing_ok=True)  # O_CREAT's mode only applies to a new file
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data))
        os.replace(tmp, path)  # never leave a half-written file to resume
        return True
    except (OSError, TypeError, ValueError) as e:
        print(f"  Could not save the conversation for --continue: {e}", file=sys.stderr)
        return False


def _archives(current: Path) -> list:
    """This project's and provider's earlier conversations, newest first."""
    return sorted(current.parent.glob(f"{current.stem}.saved-*.json"), reverse=True)


def _read(path: Path):
    """The saved data at `path` if it is a valid conversation, else None."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if _valid(data) else None


def _archive(current: Path) -> None:
    """Move the current conversation aside and keep the newest SESSION_KEEP unnamed
    ones. A named one is kept: its name is how the user means to come back to it."""
    if not current.is_file():
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    os.replace(current, current.with_name(f"{current.stem}.saved-{stamp}.json"))
    unnamed = [p for p in _archives(current) if not (_read(p) or {}).get("name")]
    for old in unnamed[config.SESSION_KEEP:]:
        old.unlink(missing_ok=True)


def clear() -> None:
    """Start a new conversation: the current one becomes an earlier one."""
    state.session_name = None
    try:
        _archive(session_file())
    except OSError:
        pass


def _bad_name(name: str) -> str | None:
    if len(name) > NAME_MAX:
        return f"A name is at most {NAME_MAX} characters."
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        return "A name is one line, without tabs or control characters."
    if name.startswith("-"):
        return "A name cannot start with -, or --resume would read it as a flag."
    return None


def rename(name: str) -> str:
    """/rename: name the current conversation, or say its name. Returns a line for the user."""
    name = name.strip()
    if not name:
        return (f"This conversation is called {state.session_name}." if state.session_name
                else "This conversation is not named. /rename <name> names it.")
    problem = _bad_name(name)
    if problem:
        return problem
    taken = find(name)
    if taken is not None and not taken["current"]:
        return f"Another conversation is already called {taken['name']}; pick another name."
    state.session_name = name
    if not save():
        return "Named, but the conversation could not be saved."
    with state.lock:
        started = bool(state.conversation_history)
    return (f"Named {name}. Return to it with /resume, or llm-light --resume {name}." if started
            else f"Named {name}; it is saved with the first turn.")


def find(name: str):
    """The saved conversation of this project and provider called `name`, ignoring
    case, as conversations() lists it; None if there is none."""
    wanted = name.strip().casefold()
    return next((e for e in conversations() if e["name"] and e["name"].casefold() == wanted), None)


def _first_question(history) -> str:
    for message in history:
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, list):
                content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
            return " ".join(str(content).split())
    return ""


def conversations() -> list:
    """The current conversation, then the earlier ones newest first; unreadable
    files are skipped. Each: path, label, first, turns, saved_at, current."""
    current = session_file()
    entries = []
    for path in [current] + _archives(current):
        data = _read(path)
        if data is None or not data["conversation_history"]:
            continue
        turns = data.get("turn_count", len(data["conversation_history"]) // 2)
        first = _first_question(data["conversation_history"])
        when = str(data.get("saved_at", "")).replace("T", " ")[:16]
        is_current = path == current
        name = data.get("name") or None
        label = ((f"{name}  ·  " if name else "")
                 + f"{when or 'undated'}  ·  {turns} turn{'' if turns == 1 else 's'}  ·  {first[:60]}"
                 + ("  (current)" if is_current else ""))
        entries.append({"path": path, "label": label, "first": first, "turns": turns, "name": name,
                        "saved_at": data.get("saved_at"), "current": is_current})
    return entries


def resume(path) -> str:
    """Make the conversation at `path` the current one and load it.

    The conversation that was current becomes an earlier one, so nothing is lost.
    """
    current = session_file()
    path = Path(path)
    if path != current:
        if not path.is_file():
            return "That conversation is no longer saved."
        try:
            _archive(current)
            os.replace(path, current)
        except OSError as e:
            return f"Could not switch conversations ({e}); nothing changed."
    return restore()


def _valid(data) -> bool:
    if not isinstance(data, dict):
        return False
    history = data.get("conversation_history")
    if not isinstance(history, list):
        return False
    for message in history:
        if not (isinstance(message, dict) and message.get("role") in _ROLES
                and isinstance(message.get("content"), (str, list))):
            return False
    return (isinstance(data.get("turn_count", 0), int)
            and isinstance(data.get("session_model"), (str, type(None)))
            and isinstance(data.get("name"), (str, type(None))))


def _other_provider_with_file(own: Path) -> str | None:
    """The provider whose saved file for this project is newest, if any."""
    best, best_time = None, -1.0
    for name in providers.NAMES:
        path = session_file(provider=name)
        if path == own:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime > best_time:
            best, best_time = name, mtime
    return best


def restore() -> str:
    """Load the saved conversation into state. Returns a line for the user."""
    path = session_file()
    if not path.is_file():
        other = _other_provider_with_file(path)
        if other:
            return (f"The saved conversation was made with {other}; starting a fresh "
                    f"{providers.active().name} conversation. "
                    f"Resume it with --provider {other} --continue.")
        return "No previous conversation for this project; starting a new one."
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return f"Could not read the saved conversation ({e}); starting a new one."
    if not _valid(data):
        return "Could not read the saved conversation (unexpected format); starting a new one."
    with state.lock:
        # In place: the package's _wrap() re-binds state.conversation_history
        # to this same list before each call, so a new list would be dropped.
        state.conversation_history[:] = data["conversation_history"]
        state.turn_count = data.get("turn_count", len(data["conversation_history"]) // 2)
        state.session_model = data.get("session_model")
        state.session_name = data.get("name") or None
    turns = state.turn_count
    when = str(data.get("saved_at", "")).replace("T", " ")[:16]
    return (f"Continuing the conversation from {when or 'an earlier session'} "
            f"({turns} turn{'' if turns == 1 else 's'}).")
