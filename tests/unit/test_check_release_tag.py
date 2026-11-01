"""
Tests for tests/linting/check_release_tag.py, the publish workflow's guard
that a GitHub release's tag names the version pyproject.toml builds. PyPI
never accepts a version twice, so a release tagged v0.1.1 that builds 0.1.0
would burn 0.1.0 under the wrong release notes, with no way to re-upload.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tests.linting.check_release_tag import main, tag_problem

ROOT = Path(__file__).resolve().parent.parent.parent


def _pyproject(version: str) -> Path:
    path = Path(tempfile.mkdtemp(prefix="cl_tag_")) / "pyproject.toml"
    path.write_text(f'[project]\nname = "x"\nversion = "{version}"\n', encoding="utf-8")
    return path


class TestTagProblem(unittest.TestCase):

    def test_tag_matching_the_version_passes(self):
        self.assertIsNone(tag_problem("v0.1.0", _pyproject("0.1.0")))

    def test_full_ref_matching_the_version_passes(self):
        self.assertIsNone(tag_problem("refs/tags/v0.1.0", _pyproject("0.1.0")))

    def test_tag_for_another_version_is_refused(self):
        problem = tag_problem("v0.1.1", _pyproject("0.1.0"))
        self.assertIn("0.1.0", problem)
        self.assertIn("v0.1.1", problem)

    def test_tag_without_the_v_prefix_is_refused(self):
        self.assertIsNotNone(tag_problem("0.1.0", _pyproject("0.1.0")))

    def test_empty_tag_is_refused(self):
        self.assertIsNotNone(tag_problem("", _pyproject("0.1.0")))


class TestMain(unittest.TestCase):

    def test_exit_code_follows_the_check(self):
        good, bad = _pyproject("2.0.0"), _pyproject("2.0.0")
        self.assertEqual(main(["check_release_tag.py", "v2.0.0", str(good)]), 0)
        self.assertEqual(main(["check_release_tag.py", "v2.0.1", str(bad)]), 1)

    def test_missing_tag_argument_fails(self):
        self.assertEqual(main(["check_release_tag.py"]), 1)

    def test_repo_pyproject_is_the_default(self):
        import tomllib
        version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        self.assertEqual(main(["check_release_tag.py", f"v{version}"]), 0)


if __name__ == "__main__":
    unittest.main()
