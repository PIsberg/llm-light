#!/usr/bin/env python3
"""Old entry point, kept so existing `python claude_light.py` commands keep working.

The project is now llm_light; this file runs llm_light.py. It will be removed in
a later release, so switch your scripts and shell aliases to llm_light.py.
"""
import runpy
import sys
from pathlib import Path

print("claude_light.py is now llm_light.py; the old name will be removed.", file=sys.stderr)
runpy.run_path(str(Path(__file__).with_name("llm_light.py")), run_name="__main__")
