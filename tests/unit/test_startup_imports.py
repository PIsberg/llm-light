"""Importing the CLI must not load what only a later step needs (#192).

A time limit would be flaky on a shared runner, so this pins the cause instead: each
module here cost 60 to 170 ms at startup, and the prompt needs none of them.
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

DEFERRED = (
    "numpy",                # retrieval and the disk cache
    "tqdm",                 # the first download of an embedding model
    "markdown_it",          # rendering the first reply
    "rich.markdown",
    "pygments",             # highlighting a diff or a new file
    "watchdog.observers",   # the file watcher, started off the prompt's thread
    "anthropic",            # the first API request
    "torch",
    "sentence_transformers",
    "prompt_toolkit",       # the interactive prompt; -p, --version and --help never show one (#200)
    "asyncio",              # MCP servers in API-key mode
)


def _loaded_after(code: str) -> list:
    script = f"{code}\nimport json, sys\nprint(json.dumps(sorted(sys.modules)))"
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                         cwd=ROOT, timeout=120, check=True).stdout
    return json.loads(out.splitlines()[-1])


def test_importing_the_cli_defers_the_heavy_modules():
    loaded = _loaded_after("import llm_light.main")
    assert [name for name in DEFERRED if name in loaded] == []


def test_watcher_class_still_resolves_on_demand():
    """Deferred, not lost: the name tests and the session use still gives the real class."""
    loaded = _loaded_after("import llm_light.main as m\nm.Observer().stop()")
    assert "watchdog.observers" in loaded


def test_the_prompt_names_still_resolve_on_demand():
    """Deferred, not lost: the completer and the prompt classes load when called."""
    loaded = _loaded_after("\n".join([
        "import llm_light.main as m",
        "from prompt_toolkit.document import Document",
        "assert [c.text for c in m._SlashCompleter().get_completions(Document('/hel'), None)] == ['/help']",
        "assert m._PROMPTTK_AVAILABLE is True",
        "m._KeyBindings(); m._AutoSuggest(); m._pt_style({'a': 'bold'})",
    ]))
    assert "prompt_toolkit" in loaded
