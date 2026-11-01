"""File edits for a release, used by the /release skill (.claude/skills/release).

    python scripts/release.py current             # version in pyproject.toml
    python scripts/release.py bump 0.1.1          # set it, date the changelog
    python scripts/release.py notes 0.1.1         # that version's changelog section

`bump` sets pyproject.toml's version, moves CHANGELOG.md's [Unreleased]
entries under "## [<version>] - <date>" and rewrites the compare links. It
refuses a version that is not X.Y.Z or not newer, and an empty [Unreleased]:
PyPI takes each version once, so a release with nothing in it wastes one.
`notes` prints the section body for `gh release create --notes-file`.
"""

import argparse
import datetime
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
CHANGELOG = ROOT / "CHANGELOG.md"
REPO_URL = "https://github.com/PIsberg/llm-light"

_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_PROJECT_VERSION = re.compile(r'^(\[project\][^\[]*?^version\s*=\s*")([^"]+)(")', re.M | re.S)


def _parts(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in version.split("."))


def _section(text: str, heading: str) -> re.Match | None:
    """Body of the "## [heading]" section, up to the next "## [" or link block."""
    return re.search(
        rf"^## \[{re.escape(heading)}\][^\n]*\n(.*?)(?=^## \[|^\[[^\]]+\]: |\Z)",
        text, re.M | re.S)


def current_version(pyproject: Path = PYPROJECT) -> str:
    return tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]


def bump(version: str, date: str, pyproject: Path = PYPROJECT, changelog: Path = CHANGELOG) -> None:
    if not _VERSION.match(version):
        raise ValueError(f"version {version!r} is not X.Y.Z")
    previous = current_version(pyproject)
    if _parts(version) <= _parts(previous):
        raise ValueError(f"version {version} is not newer than pyproject.toml's {previous}")

    log = changelog.read_text(encoding="utf-8")
    unreleased = _section(log, "Unreleased")
    if unreleased is None:
        raise ValueError("CHANGELOG.md has no ## [Unreleased] section")
    body = unreleased.group(1).strip()
    if not body:
        raise ValueError("nothing under ## [Unreleased] in CHANGELOG.md; add the entries first")

    released = f"## [Unreleased]\n\n## [{version}] - {date}\n\n{body}\n\n"
    log = log[:unreleased.start()] + released + log[unreleased.end():]
    old_link = f"[Unreleased]: {REPO_URL}/compare/v{previous}...HEAD\n"
    if old_link not in log:
        raise ValueError(f"CHANGELOG.md lacks the link line {old_link.strip()!r}")
    log = log.replace(old_link, f"[Unreleased]: {REPO_URL}/compare/v{version}...HEAD\n"
                                f"[{version}]: {REPO_URL}/compare/v{previous}...v{version}\n")

    project = pyproject.read_text(encoding="utf-8")
    project, count = _PROJECT_VERSION.subn(rf"\g<1>{version}\g<3>", project, count=1)
    if count != 1:
        raise ValueError("no version line under [project] in pyproject.toml")

    changelog.write_text(log, encoding="utf-8")
    pyproject.write_text(project, encoding="utf-8")


def notes(version: str, changelog: Path = CHANGELOG) -> str:
    section = _section(changelog.read_text(encoding="utf-8"), version)
    if section is None or not section.group(1).strip():
        raise ValueError(f"CHANGELOG.md has no entries under ## [{version}]")
    return section.group(1).strip()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="release.py", description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["current", "bump", "notes"])
    parser.add_argument("version", nargs="?")
    parser.add_argument("--date", default=datetime.date.today().isoformat())
    parser.add_argument("--pyproject", type=Path, default=PYPROJECT)
    parser.add_argument("--changelog", type=Path, default=CHANGELOG)
    args = parser.parse_args(argv[1:])
    try:
        if args.command == "current":
            print(current_version(args.pyproject))
        elif args.version is None:
            raise ValueError(f"{args.command} needs a version")
        elif args.command == "bump":
            bump(args.version, args.date, args.pyproject, args.changelog)
            print(f"pyproject.toml and CHANGELOG.md set to {args.version}")
        else:
            sys.stdout.write(notes(args.version, args.changelog) + "\n")
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
