import importlib.util
import os
import queue
import sys
import io
import threading
import time
import contextlib
import warnings
import logging

os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")
os.environ.setdefault("HF_HUB_VERBOSITY", "error")
logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
warnings.filterwarnings(
    "ignore",
    message=".*unauthenticated requests.*",
    category=UserWarning,
    module="huggingface_hub.*",
)

from pathlib import Path
from llm_light.config import TARGET_RETRIEVED_TOKENS, env_get
from llm_light.ui import _Spinner, _T_RAG, _T_ERR, _ANSI_GREEN, _ANSI_BOLD, _ANSI_RESET, _ANSI_DIM, _ANSI_YELLOW
import llm_light.state as state

# Sentinel — None until first model load.  Tests that patch
# `llm_light.SentenceTransformer` propagate here via _MODULE_ATTR_MAP so
# _load_embedding_model sees the mock instead of doing a real import.
SentenceTransformer = None

# None until a first download draws its bar: importing tqdm costs about 60 ms at startup.
tqdm = None
_TQDM_AVAILABLE = importlib.util.find_spec("tqdm") is not None

_RUN_HEAD_LINES   = 20
_RUN_TAIL_LINES   = 80
_RUN_MAX_CHARS    = 8_000
# How long to keep reading once the command itself has exited. A background child
# (`!server &`) inherits the pipe and may hold it open for as long as it lives.
_RUN_DRAIN_SECS   = 0.5
# Wall-clock cap for `/run <cmd>`. Without it a hanging command (a server,
# `tail -f`, an interactive prompt) blocks the whole chat loop indefinitely.
# Override with LLM_LIGHT_RUN_TIMEOUT (seconds); 0/blank disables the cap.
def _resolve_run_timeout():
    raw = env_get("RUN_TIMEOUT", "").strip()
    if not raw:
        return 120
    try:
        val = float(raw)
    except ValueError:
        return 120
    return val if val > 0 else None

_RUN_TIMEOUT_SECS = _resolve_run_timeout()

# Model sizes in MB (approximate for common models)
_MODEL_SIZES = {
    "all-MiniLM-L6-v2": 22,
    "all-mpnet-base-v2": 420,
    "nomic-ai/nomic-embed-text-v1.5": 550,
    "sentence-transformers/static-retrieval-mrl-en-v1": 121,
}


def _get_model_cache_dir() -> Path:
    """Get the huggingface cache directory."""
    cache_home = os.environ.get("HF_HOME")
    if cache_home:
        return Path(cache_home) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def _check_model_cached(model_name: str) -> bool:
    """Check if model is already cached locally."""
    cache_dir = _get_model_cache_dir()
    # Model names are sanitized by huggingface (slashes become hyphens)
    sanitized_name = model_name.replace("/", "--")
    model_path = cache_dir / f"models--{sanitized_name}"
    return model_path.exists()


def _detect_device() -> tuple[str, str]:
    """Return (device, hint). Hint is non-empty when torch is CPU-only
    on a machine that has CUDA-capable hardware — the user can install
    the CUDA wheel for a large speedup."""
    try:
        import torch
    except ImportError:
        return "cpu", ""

    if torch.cuda.is_available():
        return "cuda", ""

    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return "mps", ""

    hint = ""
    try:
        import shutil
        if getattr(torch.version, "cuda", None) is None and shutil.which("nvidia-smi"):
            hint = (
                "NVIDIA GPU detected but PyTorch is CPU-only — "
                "embedding will run on CPU. For a 20-100x speedup:\n"
                "    pip install torch --index-url https://download.pytorch.org/whl/cu121"
            )
    except Exception:
        pass
    return "cpu", hint


def _load_embedding_model(model_name: str, quiet: bool = False):
    """Load embedding model; heavy ML imports are deferred to here."""
    global SentenceTransformer
    # Snapshot into a local so that a concurrent test patch restoring the
    # module-level sentinel to None cannot cause a TypeError on the call below.
    _cls = SentenceTransformer
    if _cls is None:
        # Real import path — sentence_transformers pulls in PyTorch, transformers,
        # tokenizers, etc. (~1.5 GB, ~2-5s). Deferred here so startup is fast.
        from sentence_transformers import SentenceTransformer as _cls
        SentenceTransformer = _cls  # cache for next call
        try:
            import transformers.utils.logging as _tf_logging
            _tf_logging.set_verbosity_error()
            # transformers 5 draws a "Loading weights" bar on every load; from
            # a background load it lands over the prompt.
            _tf_logging.disable_progress_bar()
        except Exception:
            pass
        # Detect device only alongside the real model load. torch's first import
        # touches importlib.metadata (entry_points discovery); doing it here
        # ensures it never runs inside a test that has Path.read_text patched.
        if state.device is None:
            state.device, _device_hint = _detect_device()
            if _device_hint and not quiet:
                print(f"{_T_RAG} {_ANSI_YELLOW}{_device_hint}{_ANSI_RESET}")

    if state.device is None:
        # Fallback: real model not loaded yet (e.g. mock/test), use CPU.
        state.device = "cpu"

    is_cached = _check_model_cached(model_name)
    model_size_mb = _MODEL_SIZES.get(model_name, 100)

    if quiet or is_cached:
        with contextlib.redirect_stderr(io.StringIO()):
            return _cls(model_name, trust_remote_code=True, device=state.device)

    # Show download progress for first-time model loads
    if _TQDM_AVAILABLE:
        print(f"\n{_T_RAG} Downloading {_ANSI_BOLD}{model_name}{_ANSI_RESET} ({model_size_mb} MB)...")

        _bar = tqdm
        if _bar is None:
            from tqdm import tqdm as _bar
        with _bar(
            total=100,
            desc=f"  {model_name}",
            unit="%",
            ncols=80,
            bar_format="{desc}: {percentage:3.0f}% {bar}",
        ) as pbar:
            with contextlib.redirect_stderr(io.StringIO()):
                model = _cls(model_name, trust_remote_code=True, device=state.device)
            pbar.update(100)

        print(f"{_T_RAG} {_ANSI_GREEN}Downloaded{_ANSI_RESET} {model_name}\n")
    else:
        print(f"\n{_T_RAG} Downloading {_ANSI_BOLD}{model_name}{_ANSI_RESET} ({model_size_mb} MB)...")
        print(f"  (This may take a minute on first run...)")
        with contextlib.redirect_stderr(io.StringIO()):
            model = _cls(model_name, trust_remote_code=True, device=state.device)
        print(f"{_T_RAG} {_ANSI_GREEN}Downloaded{_ANSI_RESET} {model_name}\n")

    return model


def _queue_lines(stream, lines) -> None:
    """Runs in a thread: a blocking read cannot notice Ctrl+C or a timeout, and
    background threads never print, so the lines go to the caller."""
    try:
        for line in stream:
            lines.put(line)
    except (OSError, ValueError):
        pass
    finally:
        lines.put(None)


def _show_output(proc, timeout) -> tuple:
    """Print the command's output as it arrives. Returns (output, exit code, timed out);
    the exit code is None after a timeout. Ctrl+C stops the command and is re-raised."""
    from llm_light.providers.runner import kill_process_tree
    import subprocess
    lines = queue.Queue()
    threading.Thread(target=_queue_lines, args=(proc.stdout, lines), daemon=True,
                     name="llm-light-run-output").start()
    deadline = None if timeout is None else time.monotonic() + timeout
    captured, exited_at, timed_out = [], None, False

    def show(line):
        print(line, end="", flush=True)
        captured.append(line)

    try:
        while True:
            if deadline is not None and time.monotonic() > deadline:
                timed_out = True
                break
            try:
                line = lines.get(timeout=0.1)
            except queue.Empty:
                if proc.poll() is not None:
                    exited_at = exited_at or time.monotonic()
                    if time.monotonic() - exited_at > _RUN_DRAIN_SECS:
                        break
                continue
            if line is None:
                break
            show(line)
        if not timed_out:
            # A command can close its stdout and go on running.
            try:
                proc.wait(timeout=None if deadline is None else max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
    except KeyboardInterrupt:
        kill_process_tree(proc)
        raise
    if not timed_out:
        return "".join(captured), proc.returncode, False
    # kill_process_tree, not proc.kill(): that ends only the shell, and on
    # Windows its child keeps the pipe open and goes on running.
    kill_process_tree(proc)
    stop_at = time.monotonic() + _RUN_DRAIN_SECS
    while time.monotonic() < stop_at:
        try:
            line = lines.get(timeout=0.1)
        except queue.Empty:
            break
        if line is None:
            break
        show(line)
    return "".join(captured), None, True


def _run_command(cmd: str) -> str:
    """Run a command the user typed (`!cmd`, `/run cmd`), showing its output as it
    arrives, and return the transcript for the model. Its stdin is closed: nothing on
    screen would say a command is waiting for input. KeyboardInterrupt stops the whole
    process tree and is re-raised, so the caller can return to the prompt."""
    return run_command_status(cmd)[0]


def run_command_status(cmd: str) -> tuple:
    """_run_command, returning (transcript, exit code). The code is None when the command
    timed out or could not start."""
    import subprocess
    from llm_light.providers.runner import new_session
    from llm_light import user_shell
    _T_RUN = f"{_ANSI_YELLOW}[Run]{_ANSI_RESET}"
    print(f"\n{_T_RUN} {_ANSI_DIM}$ {cmd}{_ANSI_RESET}")
    print(f"{_ANSI_DIM}{'-'*60}{_ANSI_RESET}")
    argv, use_shell = user_shell.command_argv(cmd)
    try:
        proc = subprocess.Popen(
            argv, shell=use_shell, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            # A piped Python buffers its stdout, which would hold back all its output.
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            # Own process group on POSIX, so a timeout can kill sh and its children.
            start_new_session=new_session(),
        )
    except Exception as exc:
        msg = f"[Run] Failed to start process: {exc}"
        print(f"{_T_ERR} {msg}")
        return msg, None

    raw, exit_code, timed_out = _show_output(proc, _RUN_TIMEOUT_SECS)
    print(f"{_ANSI_DIM}{'-'*60}{_ANSI_RESET}")
    if timed_out:
        print(f"{_T_RUN} {_ANSI_YELLOW}Timed out after {_RUN_TIMEOUT_SECS}s "
              f"(process killed){_ANSI_RESET}\n")
    else:
        status_color = _ANSI_GREEN if exit_code == 0 else "\033[31m" # RED
        print(f"{_T_RUN} Exit code: {status_color}{exit_code}{_ANSI_RESET}\n")

    lines = raw.splitlines()
    if len(lines) <= _RUN_HEAD_LINES + _RUN_TAIL_LINES:
        trimmed = raw
    else:
        head = "\n".join(lines[:_RUN_HEAD_LINES])
        tail = "\n".join(lines[-_RUN_TAIL_LINES:])
        omitted = len(lines) - _RUN_HEAD_LINES - _RUN_TAIL_LINES
        trimmed = f"{head}\n\n... ({omitted} lines omitted) ...\n\n{tail}"

    if len(trimmed) > _RUN_MAX_CHARS:
        trimmed = trimmed[-_RUN_MAX_CHARS:]
        trimmed = f"... (truncated to last {_RUN_MAX_CHARS} chars) ...\n" + trimmed

    status_line = (f"[TIMEOUT after {_RUN_TIMEOUT_SECS}s]"
                   if timed_out else f"(exit {exit_code})")
    return f"$ {cmd}\n{status_line}\n{trimmed}", exit_code

def auto_tune(source_files, chunks=None, quiet=False, load_model=True):
    n = len(source_files)
    if n < 50:
        chosen_model = "all-MiniLM-L6-v2"
    elif n < 200:
        chosen_model = "all-mpnet-base-v2"
    else:
        chosen_model = "nomic-ai/nomic-embed-text-v1.5"

    # Only load model if explicitly requested AND (model changed or not loaded)
    # This prevents loading on warm cache hits
    if load_model and (chosen_model != state.EMBED_MODEL or state.embedder is None):
        state.EMBED_MODEL = chosen_model
        state.embedder = _load_embedding_model(state.EMBED_MODEL, quiet=quiet)
        state.embedder_ready.set()
        if not quiet:
            print(f"{_T_RAG} {_ANSI_GREEN}Loaded{_ANSI_RESET} {chosen_model}")
    elif load_model:
        # Already loaded — just mark ready (idempotent for callers)
        state.embedder_ready.set()
    else:
        # Just update the model choice without loading
        state.EMBED_MODEL = chosen_model

    if chunks:
        n_units     = len(chunks)
        total_chars = sum(len(c["text"]) for c in chunks)
    else:
        n_units     = n
        total_chars = sum(f.stat().st_size for f in source_files if f.exists())

    avg_tokens = set_top_k(total_chars, n_units)

    unit_label = f"{n_units} chunks" if chunks else f"{n} files"
    if not quiet:
        print(
            f"{_T_RAG} Auto-tune → {_ANSI_BOLD}{n} files{_ANSI_RESET} → {unit_label} | "
            f"~{avg_tokens} tok/chunk | TOP_K={_ANSI_BOLD}{state.TOP_K}{_ANSI_RESET} | model={state.EMBED_MODEL}"
        )


def set_top_k(total_chars: int, n_units: int) -> int:
    """Set state.TOP_K so TOP_K chunks fill TARGET_RETRIEVED_TOKENS; returns
    the average chunk size in tokens."""
    avg_tokens = max(1, (total_chars // n_units) // 4) if n_units else 1
    state.TOP_K = max(2, min(15, round(TARGET_RETRIEVED_TOKENS / avg_tokens)))
    return avg_tokens


def start_embedder_background_load(quiet: bool = False) -> threading.Thread:
    """Load state.EMBED_MODEL in a daemon thread; set state.embedder_ready when done.

    Used on full cache hits: the model is only needed for query encoding, so we
    return control to the caller immediately and let retrieve() block on the
    event if the user's first query arrives before the load finishes.
    """
    state.embedder_ready.clear()
    state.embedder_load_error = None
    model_name = state.EMBED_MODEL

    def _load():
        try:
            state.embedder = _load_embedding_model(model_name, quiet=True)
        except Exception as exc:
            state.embedder_load_error = exc
        finally:
            state.embedder_ready.set()

    t = threading.Thread(target=_load, daemon=True, name="llm-light-embedder")
    t.start()
    return t
