#!/usr/bin/env python3
"""Validate that all modified files have correct Python syntax."""

import sys
import py_compile
from pathlib import Path

files_to_check = [
    "llm_light/git_manager.py",
    "llm_light/editor.py",
    "llm_light/llm.py",
    "llm_light/main.py",
    "llm_light/testing.py",
    "tests/unit/test_llm_light.py",
]

# Go up 3 levels from tests/linting/
project_root = Path(__file__).resolve().parent.parent.parent
errors = []

for file_path in files_to_check:
    full_path = project_root / file_path
    if not full_path.exists():
        print(f"✗ File not found: {file_path}")
        errors.append(f"Missing: {file_path}")
        continue
    
    try:
        py_compile.compile(str(full_path), doraise=True)
        print(f"✓ {file_path} — syntax OK")
    except py_compile.PyCompileError as e:
        print(f"✗ {file_path} — syntax error:")
        print(f"  {e}")
        errors.append(f"Syntax error in {file_path}: {e}")

if errors:
    print(f"\n{len(errors)} error(s) found:")
    for err in errors:
        print(f"  - {err}")
    sys.exit(1)
else:
    print(f"\n✓ All {len(files_to_check)} files have valid Python syntax")
    sys.exit(0)
