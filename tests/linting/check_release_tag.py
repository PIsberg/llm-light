"""Refuse to publish when the release tag does not name the built version.

PyPI accepts each version exactly once, even after a delete. A GitHub release
tagged v0.1.1 whose pyproject.toml still says 0.1.0 would upload 0.1.0 under
the wrong release notes and leave no way to fix it. publish.yml runs this
before it builds.

    python tests/linting/check_release_tag.py v0.1.0 [path/to/pyproject.toml]
"""

import sys
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent.parent.parent / "pyproject.toml"


def tag_problem(tag: str, pyproject: Path = PYPROJECT) -> str | None:
    """None when tag is v<version>, else the reason it is not."""
    version = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    tag = tag.removeprefix("refs/tags/")
    expected = f"v{version}"
    if tag != expected:
        return (f"release tag {tag or '(empty)'!r} does not match pyproject.toml "
                f"version {version}; expected {expected!r}")
    return None


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: check_release_tag.py <tag> [pyproject.toml]", file=sys.stderr)
        return 1
    problem = tag_problem(argv[1], Path(argv[2]) if len(argv) > 2 else PYPROJECT)
    if problem:
        print(f"ERROR: {problem}", file=sys.stderr)
        return 1
    print(f"OK: {argv[1]} matches pyproject.toml")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
