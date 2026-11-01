"""
Tests for scripts/release.py, the file edits behind the /release skill: set
the version in pyproject.toml, move CHANGELOG.md's [Unreleased] entries under
the new version, and print a version's section as the GitHub release notes.
A hand edit that missed one of these would publish a version whose tag check,
changelog or release notes disagree, and PyPI never takes the version back.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.release import bump, current_version, main, notes

REPO = "https://github.com/PIsberg/llm-light"

PYPROJECT = """[build-system]
requires = ["setuptools>=84.2.1"]

[project]
name = "llm-light"
version = "0.1.0"
dependencies = [
    "rich>=15.0.0",
]
"""

CHANGELOG = f"""# Changelog

Intro.

## [Unreleased]

### Fixed

- A fix.

## [0.1.0]

The first release.

[Unreleased]: {REPO}/compare/v0.1.0...HEAD
[0.1.0]: {REPO}/releases/tag/v0.1.0
"""


class _Files(unittest.TestCase):

    def setUp(self):
        d = Path(tempfile.mkdtemp(prefix="cl_release_"))
        self.pyproject = d / "pyproject.toml"
        self.changelog = d / "CHANGELOG.md"
        self.pyproject.write_text(PYPROJECT, encoding="utf-8")
        self.changelog.write_text(CHANGELOG, encoding="utf-8")

    def bump(self, version, date="2026-11-02"):
        bump(version, date, self.pyproject, self.changelog)


class TestBump(_Files):

    def test_sets_the_project_version_only(self):
        self.bump("0.1.1")
        text = self.pyproject.read_text(encoding="utf-8")
        self.assertIn('version = "0.1.1"', text)
        self.assertIn('requires = ["setuptools>=84.2.1"]', text)
        self.assertEqual(current_version(self.pyproject), "0.1.1")

    def test_moves_unreleased_entries_under_the_dated_version(self):
        self.bump("0.1.1")
        text = self.changelog.read_text(encoding="utf-8")
        self.assertIn("## [Unreleased]\n\n## [0.1.1] - 2026-11-02\n\n### Fixed\n\n- A fix.\n\n## [0.1.0]", text)
        self.assertEqual(text.count("- A fix."), 1)

    def test_rewrites_the_compare_links(self):
        self.bump("0.2.0")
        text = self.changelog.read_text(encoding="utf-8")
        self.assertIn(f"[Unreleased]: {REPO}/compare/v0.2.0...HEAD\n", text)
        self.assertIn(f"[0.2.0]: {REPO}/compare/v0.1.0...v0.2.0\n", text)
        self.assertIn(f"[0.1.0]: {REPO}/releases/tag/v0.1.0\n", text)

    def test_refuses_a_version_that_is_not_newer(self):
        for version in ("0.1.0", "0.0.9"):
            with self.assertRaises(ValueError):
                self.bump(version)
        self.assertEqual(self.pyproject.read_text(encoding="utf-8"), PYPROJECT)

    def test_compares_versions_as_numbers(self):
        self.pyproject.write_text(PYPROJECT.replace("0.1.0", "0.9.0"), encoding="utf-8")
        self.changelog.write_text(CHANGELOG.replace("0.1.0", "0.9.0"), encoding="utf-8")
        self.bump("0.10.0")
        self.assertEqual(current_version(self.pyproject), "0.10.0")

    def test_refuses_a_malformed_version(self):
        for version in ("v0.1.1", "0.1", "0.1.1rc1", ""):
            with self.assertRaises(ValueError):
                self.bump(version)

    def test_refuses_an_empty_unreleased_section(self):
        self.changelog.write_text(CHANGELOG.replace("### Fixed\n\n- A fix.\n\n", ""), encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            self.bump("0.1.1")
        self.assertIn("Unreleased", str(caught.exception))
        self.assertEqual(self.pyproject.read_text(encoding="utf-8"), PYPROJECT)


class TestNotes(_Files):

    def test_prints_the_section_body(self):
        self.assertEqual(notes("0.1.0", self.changelog), "The first release.")

    def test_reads_a_dated_heading(self):
        self.bump("0.1.1")
        self.assertEqual(notes("0.1.1", self.changelog), "### Fixed\n\n- A fix.")

    def test_refuses_a_version_with_no_section(self):
        with self.assertRaises(ValueError):
            notes("9.9.9", self.changelog)


class TestMain(_Files):

    def test_errors_exit_1(self):
        self.assertEqual(main(["release.py", "bump", "0.0.1", "--pyproject", str(self.pyproject),
                               "--changelog", str(self.changelog)]), 1)
        self.assertEqual(main(["release.py", "notes", "9.9.9", "--changelog", str(self.changelog)]), 1)

    def test_bump_then_notes_succeed(self):
        files = ["--pyproject", str(self.pyproject), "--changelog", str(self.changelog)]
        self.assertEqual(main(["release.py", "bump", "0.1.1", "--date", "2026-11-02", *files]), 0)
        self.assertEqual(main(["release.py", "notes", "0.1.1", *files]), 0)
        self.assertEqual(main(["release.py", "current", *files]), 0)

    def test_repo_changelog_has_notes_for_the_repo_version(self):
        self.assertEqual(main(["release.py", "notes", current_version()]), 0)


if __name__ == "__main__":
    unittest.main()
