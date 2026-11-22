"""
Tests for llm_light.git_manager — isolated from the real repo via mocked
subprocess.run calls.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy-test-key")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from llm_light import git_manager


class TestGetModifiedFiles(unittest.TestCase):
    """get_modified_files parses `git status --porcelain -z`: NUL-terminated
    records, read without the strip that would eat the first record's
    leading space."""

    def _mock_git_output(self, stdout: str, returncode: int = 0):
        mock = MagicMock()
        mock.stdout = stdout
        mock.returncode = returncode
        return mock

    def test_single_unstaged_modification_does_not_truncate_path(self):
        print("\n  ▶ TestGetModifiedFiles.test_single_unstaged_modification_does_not_truncate_path")
        # `git status --porcelain` for an unstaged edit emits " M path".
        # A strip of the whole output would remove the first record's
        # leading space, and slicing at 3 would then return
        # "ocs/architecture.md". Regression test for that bug.
        with patch("llm_light.git_manager.subprocess.run",
                   return_value=self._mock_git_output(" M docs/architecture.md\0")):
            files = git_manager.get_modified_files()
        self.assertEqual(files, ["docs/architecture.md"])

    def test_multiple_files_all_preserved(self):
        print("\n  ▶ TestGetModifiedFiles.test_multiple_files_all_preserved")
        stdout = " M a.py\0 M b.py\0MM c.py\0"
        with patch("llm_light.git_manager.subprocess.run",
                   return_value=self._mock_git_output(stdout)):
            files = git_manager.get_modified_files()
        self.assertEqual(files, ["a.py", "b.py", "c.py"])

    def test_staged_new_file(self):
        print("\n  ▶ TestGetModifiedFiles.test_staged_new_file")
        with patch("llm_light.git_manager.subprocess.run",
                   return_value=self._mock_git_output("A  new_module.py\0")):
            files = git_manager.get_modified_files()
        self.assertEqual(files, ["new_module.py"])

    def test_empty_output_returns_empty_list(self):
        print("\n  ▶ TestGetModifiedFiles.test_empty_output_returns_empty_list")
        with patch("llm_light.git_manager.subprocess.run",
                   return_value=self._mock_git_output("")):
            files = git_manager.get_modified_files()
        self.assertEqual(files, [])

    def test_nonzero_returncode_returns_empty_list(self):
        print("\n  ▶ TestGetModifiedFiles.test_nonzero_returncode_returns_empty_list")
        with patch("llm_light.git_manager.subprocess.run",
                   return_value=self._mock_git_output("", returncode=128)):
            files = git_manager.get_modified_files()
        self.assertEqual(files, [])

    def test_path_with_subdirectories(self):
        print("\n  ▶ TestGetModifiedFiles.test_path_with_subdirectories")
        with patch("llm_light.git_manager.subprocess.run",
                   return_value=self._mock_git_output(" M a/b/c/d.py\0")):
            files = git_manager.get_modified_files()
        self.assertEqual(files, ["a/b/c/d.py"])


class TestModifiedFilesInRealRepo(unittest.TestCase):
    """Against a real repository: git quotes a path with a space or a
    non-ASCII character in plain --porcelain output, and reports a new
    directory as one entry. Those strings are not file names (#269)."""

    def setUp(self):
        import subprocess
        import tempfile
        self.tmpdir = tempfile.mkdtemp()
        self.orig = os.getcwd()
        os.chdir(self.tmpdir)
        run = lambda *a: subprocess.run(["git", *a], capture_output=True, check=True)
        run("init", "-q")
        run("config", "user.email", "test@example.com")
        run("config", "user.name", "Test User")
        run("config", "core.quotePath", "true")
        self.cafe = "caf" + chr(233) + ".py"
        for name in ("my file.py", self.cafe, "old.py"):
            Path(name).write_text("x = 1\n", encoding="utf-8")
        run("add", "-A")
        run("commit", "-q", "-m", "init")
        self.run_git = run

    def tearDown(self):
        os.chdir(self.orig)
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _agent_edits(self):
        Path("my file.py").write_text("x = 2\n", encoding="utf-8")
        Path(self.cafe).write_text("x = 2\n", encoding="utf-8")
        Path("newpkg").mkdir()
        Path("newpkg", "mod.py").write_text("y = 1\n", encoding="utf-8")

    def test_names_are_real_paths(self):
        print("\n  ▶ TestModifiedFilesInRealRepo.test_names_are_real_paths")
        self._agent_edits()
        files = sorted(git_manager.get_modified_files())
        self.assertEqual(files, sorted(["my file.py", self.cafe, "newpkg/mod.py"]))
        for f in files:
            self.assertTrue(os.path.isfile(f), f)

    def test_rename_reports_the_new_path(self):
        print("\n  ▶ TestModifiedFilesInRealRepo.test_rename_reports_the_new_path")
        self.run_git("mv", "old.py", "new name.py")
        self.assertEqual(git_manager.get_modified_files(), ["new name.py"])

    def test_tree_state_sees_the_files(self):
        print("\n  ▶ TestModifiedFilesInRealRepo.test_tree_state_sees_the_files")
        self._agent_edits()
        state = git_manager.tree_state()
        self.assertEqual(set(state["files"]), {"my file.py", self.cafe, "newpkg/mod.py"})
        self.assertTrue(all(v is not None for v in state["files"].values()))

    def test_agent_edits_are_committed(self):
        print("\n  ▶ TestModifiedFilesInRealRepo.test_agent_edits_are_committed")
        import io
        import subprocess
        from llm_light import llm
        before = llm._git_modified_snapshot()
        self._agent_edits()
        changed = set(git_manager.get_modified_files()) - set(before)
        with patch("sys.stdout", io.StringIO()):
            llm._commit_agent_edits(changed, "Agent edit", auto_apply=True)
        status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True).stdout
        self.assertEqual(status, "")
        shown = subprocess.run(["git", "show", "--name-only", "--format=", "-z", "HEAD"],
                               capture_output=True).stdout.decode("utf-8").split("\0")
        self.assertEqual(sorted(p for p in shown if p), sorted(["my file.py", self.cafe, "newpkg/mod.py"]))


class TestAutoCommitFailures(unittest.TestCase):
    """auto_commit against a real repository: an ignored file among the
    edits, and a pre-commit hook that rejects the commit (#276)."""

    def setUp(self):
        import subprocess
        import tempfile
        self.tmpdir = tempfile.mkdtemp()
        self.orig = os.getcwd()
        os.chdir(self.tmpdir)
        self.git = lambda *a: subprocess.run(["git", *a], capture_output=True, text=True)
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test User")
        Path(".gitignore").write_text("local_settings.py\n", encoding="utf-8")
        Path("a.py").write_text("x = 1\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")

    def tearDown(self):
        os.chdir(self.orig)
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _commit(self, files):
        import io
        out = io.StringIO()
        with patch("sys.stdout", out):
            ok = git_manager.auto_commit(files, "Claude edit")
        return ok, out.getvalue()

    def test_ignored_file_is_left_out_and_the_rest_committed(self):
        print("\n  ▶ TestAutoCommitFailures.test_ignored_file_is_left_out_and_the_rest_committed")
        Path("a.py").write_text("x = 2\n", encoding="utf-8")
        Path("local_settings.py").write_text("SECRET = 1\n", encoding="utf-8")
        ok, printed = self._commit(["a.py", "local_settings.py"])
        self.assertTrue(ok, printed)
        self.assertIn("local_settings.py", printed)
        self.assertIn("ignored", printed)
        self.assertEqual(self.git("status", "--porcelain").stdout.strip(), "")
        self.assertEqual(self.git("show", "--name-only", "--format=", "HEAD").stdout.split(), ["a.py"])

    def test_only_ignored_files_means_no_commit(self):
        print("\n  ▶ TestAutoCommitFailures.test_only_ignored_files_means_no_commit")
        # False: the caller then offers no revert of "the last commit", an older one.
        Path("local_settings.py").write_text("SECRET = 1\n", encoding="utf-8")
        head = self.git("rev-parse", "HEAD").stdout
        ok, printed = self._commit(["local_settings.py"])
        self.assertFalse(ok)
        self.assertIn("ignored", printed)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout, head)

    def test_rejected_commit_says_why_and_unstages(self):
        print("\n  ▶ TestAutoCommitFailures.test_rejected_commit_says_why_and_unstages")
        hook = Path(".git", "hooks", "pre-commit")
        hook.write_text("#!/bin/sh\necho 'lint: a.py line 1 uses a magic number' >&2\nexit 1\n",
                        encoding="utf-8", newline="\n")
        os.chmod(hook, 0o755)
        Path("a.py").write_text("x = 2\n", encoding="utf-8")
        ok, printed = self._commit(["a.py"])
        self.assertFalse(ok)
        self.assertIn("magic number", printed)
        # Nothing this call staged stays staged; the edit stays on disk.
        self.assertEqual(self.git("diff", "--cached", "--name-only").stdout.strip(), "")
        self.assertEqual(Path("a.py").read_text(encoding="utf-8"), "x = 2\n")


if __name__ == "__main__":
    unittest.main()
