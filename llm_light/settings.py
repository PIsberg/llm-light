"""~/.llm_light/settings.json: what should survive a restart, and /config.

Only under the home directory, never in the project: a file a cloned repo controls must
not choose a permission mode. `bypassPermissions` is not offered, because it takes a flag
each time, so nothing can leave it on quietly. A command-line flag beats a setting, and a
damaged or hand-edited value is ignored.
"""

import json
import os
from pathlib import Path

import llm_light.state as state
from llm_light import commands, config, permissions, providers
from llm_light.providers.base import EFFORT, TOP_TIER

_CHOICES = {
    "vim": ("off", "on"),
    "theme": ("dark", "light"),
    "notify": ("off", "bell"),
    "permission_mode": (permissions.DEFAULT, permissions.ACCEPT_EDITS, permissions.PLAN),
    "model": (*commands.MODEL_ALIASES, "auto"),
    "effort": (*commands.EFFORTS, "auto"),
}
_ABOUT = {
    "vim": "vi keys at the prompt",
    "theme": "colours of code and diffs; light is for a light terminal background",
    "notify": "ring the terminal bell when a turn of 15 s or more ends",
    "permission_mode": "the mode a session starts in",
    "model": "pin every question to this model",
    "effort": "pin every question to this effort",
}


def _file() -> Path:
    return Path(config.env_get("SETTINGS_FILE") or Path.home() / ".llm_light" / "settings.json")


def load() -> dict:
    """The valid settings in the file."""
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k in _CHOICES and v in _CHOICES[k]}


def _save(values: dict) -> None:
    path = _file()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(values, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _apply(key: str, value: str) -> None:
    if key == "vim":
        state.vim_mode = value == "on"
    elif key == "theme":
        state.theme = value
    elif key == "notify":
        state.notify = value
    elif key == "permission_mode":
        permissions.set_mode(value)
    elif key == "model":
        state.pinned_model = None if value == "auto" else commands.resolve_model(value)
    elif key == "effort":
        state.pinned_effort = None if value == "auto" else value


def apply_at_startup(provider, skip=()) -> None:
    """Apply the saved settings, except those a flag already set (`skip`) and those the
    provider has no use for: it picks its own model, so that is not worth a warning."""
    unavailable = set()
    if not provider.has(TOP_TIER):
        unavailable.add("model")
    if not provider.has(EFFORT):
        unavailable.add("effort")
    for key, value in load().items():
        if key not in skip and key not in unavailable:
            _apply(key, value)


def config_command(arg: str) -> str:
    """`/config`: list the settings. `/config <key> <value>`: save one and apply it now."""
    key, _, value = arg.strip().partition(" ")
    value = value.strip()
    current = load()
    if not key:
        lines = [f"Settings in {_file()}. /config <key> <value> saves one; flags beat them."]
        for name, choices in _CHOICES.items():
            lines.append(f"    {name:<16}{current.get(name, 'not set'):<14}{'|'.join(choices)}: {_ABOUT[name]}")
        return "\n".join(lines)
    if key not in _CHOICES:
        return f"Unknown setting {key!r}. Settings: {', '.join(_CHOICES)}."
    if value not in _CHOICES[key]:
        return f"{key} can be {', '.join(_CHOICES[key])}."
    try:
        _save({**current, key: value})
    except OSError as e:
        return f"Could not save it: {e}."
    _apply(key, value)
    if key == "model" and not providers.active().has(TOP_TIER):
        return f"Saved {key} = {value}. {providers.active().name} picks its own model, so it applies to Claude only."
    return f"Saved {key} = {value}, from now on and in later sessions."
