"""CLI subprocess runner shared by the CLI providers."""
import contextlib
import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

from llm_light.providers.base import ProviderError

TIMEOUT_SECS = 600

# signal.SIGKILL does not exist on Windows; it is only used on POSIX.
_SIGKILL = getattr(signal, "SIGKILL", 9)


def new_session() -> bool:
    """Popen's start_new_session for a CLI child: its own process group on
    POSIX, so kill_process_tree can stop everything it starts.

    It also drops the controlling terminal, which agy depends on: signed out and
    with a terminal, agy 1.4.12 waits for a browser sign-in; without one it exits
    at once with an error (Linux, setsid vs not, #124). Windows does not detach.
    """
    return os.name != "nt"


@contextlib.contextmanager
def empty_temp_dir():
    """A fresh empty directory, removed on exit (also when the body raises).

    Answer-engine CLIs run here so they cannot read a hostile repo's config
    (hooks, discovery commands) from the project directory.
    """
    path = tempfile.mkdtemp(prefix="llm_light_cli_")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def run_cli(provider: str, argv: list, stdin_text: str, timeout: float = TIMEOUT_SECS,
            cwd=None, env=None) -> str:
    """Run a provider CLI, feed it stdin_text and return its stdout.

    env replaces the child's environment when given (None inherits ours).

    A non-zero exit, a timeout or a missing binary raise ProviderError naming
    the provider. KeyboardInterrupt stops the whole process tree and is re-raised.
    """
    binary = Path(argv[0]).stem if argv else provider
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", cwd=cwd, env=env,
            start_new_session=new_session())
    except FileNotFoundError:
        raise ProviderError(f"{binary} CLI not found. Install it and log in, "
                            f"or choose another --provider.")
    try:
        stdout, stderr = proc.communicate(stdin_text, timeout)
    except subprocess.TimeoutExpired:
        kill_process_tree(proc)
        raise ProviderError(f"{provider} timed out after {timeout:g} s. "
                            f"Run {binary} on its own to see why.")
    except KeyboardInterrupt:
        kill_process_tree(proc)
        raise
    if proc.returncode != 0:
        lines = [ln.strip() for ln in (stderr or "").splitlines()
                 if ln.strip() and not ln.strip().startswith("at ")]
        errors = [ln for ln in lines if "Error" in ln]
        last = errors[-1] if errors else (lines[-1] if lines else "no error output")
        raise ProviderError(f"{provider} failed (exit {proc.returncode}): {last}. "
                            f"Run {binary} on its own to see the full error.")
    return stdout


def kill_process_tree(proc) -> None:
    """Stop the CLI and everything it started.

    On Windows the CLI runs via cmd.exe (shell=True), so proc.kill() only
    ends the shell: the CLI keeps running, keeps editing files, and keeps
    the stdout pipe open so the read loop never finishes. On POSIX a wrapper
    (the codex npm script, sh for shell=True) starts the real binary as a
    child, so the whole process group is killed. That needs the child to
    lead its own group (Popen(start_new_session=new_session())); a child
    in our group is killed alone, because killpg would end llm_light too.
    """
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, stdin=subprocess.DEVNULL,
            )
        elif os.getpgid(proc.pid) == proc.pid:
            os.killpg(proc.pid, _SIGKILL)
        else:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    # Reap it, so it does not linger as a zombie on POSIX.
    try:
        proc.wait(timeout=5)
    except Exception:
        pass
