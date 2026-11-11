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


# --- the background work waits for the first frame -----------------------------------------

def _start_chat_recording(monkeypatch, answers):
    """Run start_chat with a stand-in prompt: each answer is a callable run at that prompt,
    which returns the line typed. Returns the log of what started and when."""
    from unittest.mock import MagicMock
    from prompt_toolkit.utils import Event
    import llm_light as cl

    log = []
    session = MagicMock()
    session.app.after_render = Event(session.app)
    calls = iter(answers)
    session.prompt.side_effect = lambda *a, **k: next(calls)(session.app, log)

    monkeypatch.setattr(cl, "full_refresh", lambda **k: log.append("index"))
    monkeypatch.setattr(cl, "Observer", lambda: log.append("watcher") or MagicMock())
    monkeypatch.setattr(cl, "SourceHandler", MagicMock)
    monkeypatch.setattr(cl, "_PROMPTTK_AVAILABLE", True)
    monkeypatch.setattr(cl, "_PromptSession", lambda **k: session)
    monkeypatch.setattr(cl, "_FileHistory", lambda path: MagicMock())
    monkeypatch.setattr(cl, "_AutoSuggest", MagicMock)
    monkeypatch.setattr(cl, "CACHE_DIR", MagicMock())
    monkeypatch.setattr(cl, "print_session_summary", lambda *a, **k: None)
    monkeypatch.setattr("threading.Thread", MagicMock())  # targets run inline
    monkeypatch.setattr("builtins.print", lambda *a, **k: None)
    cl.start_chat(load_compressor=lambda: log.append("llmlingua"))
    return log


def test_indexing_and_loaders_start_after_the_first_frame(monkeypatch):
    # Started before it, their imports (the embedder, transformers, watchdog) contend for
    # the GIL with the prompt's own: on Windows the first frame came about 200 ms later.
    def first_prompt(app, log):
        log.append("prompt built")
        app.after_render.fire()
        log.append("frame drawn")
        app.after_render.fire()  # every later frame changes nothing
        return "exit"
    log = _start_chat_recording(monkeypatch, [first_prompt])
    assert log == ["prompt built", "index", "watcher", "llmlingua", "frame drawn"]


def test_they_start_before_a_question_even_without_a_frame(monkeypatch):
    def typed(app, log):
        log.append("question typed")
        return "/help"

    def second(app, log):
        log.append("second prompt")
        return "exit"
    log = _start_chat_recording(monkeypatch, [typed, second])
    assert log == ["question typed", "index", "watcher", "llmlingua", "second prompt"]


def _main_loader_calls(monkeypatch, argv, isatty):
    """main() with the session and the turn stubbed: what it did with the LLMLingua loader."""
    import llm_light.main as main_mod
    seen = {"started": 0}

    def loader():
        seen["started"] += 1

    monkeypatch.setattr(main_mod, "_start_llmlingua_load", loader)
    monkeypatch.setattr(main_mod, "start_chat", lambda **k: seen.update(handed=k.get("load_compressor")))
    monkeypatch.setattr(main_mod, "one_shot", lambda *a, **k: None)
    monkeypatch.setattr(main_mod.sys, "argv", ["llm_light", *argv])
    monkeypatch.setattr(main_mod.sys.stdin, "isatty", lambda: isatty)
    main_mod.main()
    return seen, loader


def test_an_interactive_start_hands_the_loader_to_the_session(monkeypatch):
    seen, loader = _main_loader_calls(monkeypatch, [], isatty=True)
    assert seen["started"] == 0 and seen["handed"] is loader


def test_a_one_shot_question_still_starts_the_loader_at_once(monkeypatch):
    seen, _ = _main_loader_calls(monkeypatch, ["-p", "what is this?"], isatty=False)
    assert seen["started"] == 1 and "handed" not in seen
