"""
The PyPI distribution name is not the import name: the project installs as
llm-light-cli (pyproject.toml's [project] name) and imports as llm_light,
because PyPI refuses "llm-light" as too similar to the existing "LLMlight".

An installed copy has no pyproject.toml, so `llm-light --version` reads its
version from the metadata of the distribution named commands.DIST_NAME. If
that constant and pyproject.toml's name disagree, --version prints "unknown"
with no error, and only on an installed copy, never in a checkout.
"""

import sys
import tomllib
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from llm_light import commands


class TestDistName(unittest.TestCase):

    def test_matches_pyproject_name(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        self.assertEqual(commands.DIST_NAME, project["name"])

    def test_installed_copy_reads_metadata_by_dist_name(self):
        """With no pyproject.toml beside the package, the version comes from
        the metadata of DIST_NAME."""
        seen = []

        def fake_version(name):
            seen.append(name)
            return "9.8.7"

        with mock.patch.object(Path, "read_text", side_effect=OSError("not a checkout")), \
             mock.patch("importlib.metadata.version", fake_version):
            self.assertEqual(commands.version(), "9.8.7")
        self.assertEqual(seen, [commands.DIST_NAME])


if __name__ == "__main__":
    unittest.main()
