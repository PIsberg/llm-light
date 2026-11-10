"""A stand-in for a turn, for tests/unit/test_esc_key.py: listen for Esc while "the model
answers", which is the main thread blocked reading an agent process's output, as a CLI turn
is, and write what happened to argv[1]. Run in a real terminal: a pty on POSIX, a console of
its own on Windows."""

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from llm_light import esc_key  # noqa: E402

out = Path(sys.argv[1])
ready = Path(sys.argv[2])
result = {"interrupted": False}
try:
    agent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                             stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
    started = time.monotonic()
    with esc_key.listening() as keys, esc_key.running(agent):
        result["listening"] = keys.active
        ready.write_text("ready", encoding="utf-8")
        for _line in agent.stdout:
            pass
except KeyboardInterrupt:
    result["interrupted"] = True
    result["fast"] = time.monotonic() - started < 10
finally:
    agent.kill()
result["typeahead"] = esc_key.take_typeahead()
if sys.platform != "win32":
    import termios
    result["echo_restored"] = bool(termios.tcgetattr(sys.stdin.fileno())[3] & termios.ECHO)
out.write_text(json.dumps(result), encoding="utf-8")
