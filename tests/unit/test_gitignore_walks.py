"""What .gitignore excludes stays out of grep, list_files, @ completion and the
affected-test search, as it does out of the skeleton and the index (#267, #325).

Those three walked the directory and skipped only SKIP_DIRS and dot names, so a
gitignored build or coverage folder filled grep and list_files up to their caps
and made the model spend rounds on narrower globs, @ completion walked it on
every keystroke, and the affected-test search ran tests generated into it.
Outside a git work tree there is no .gitignore to honour, and the walk is as before.
"""

import subprocess
from pathlib import Path

import pytest

from llm_light import affected_tests, commands, tools


def _git(root, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root,
                   check=True, capture_output=True)


def _write(root, rel, text):
    path = Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _project(root, git):
    _write(root, "src/service.py", "def needle():\n    pass\n")
    _write(root, "tests/test_service.py", "from src import service\n")
    _write(root, "build_out/gen.py", "def needle():\n    pass\n")             # gitignored folder
    _write(root, "build_out/test_gen.py", "from src import service\n")
    _write(root, "debug.log", "needle\n")                                      # gitignored file
    _write(root, "notes_new.py", "needle = 1\n")                               # untracked, not ignored
    _write(root, ".gitignore", "build_out/\n*.log\n")
    if git:
        _git(root, "init", "-q")
        _git(root, "add", "src", "tests", ".gitignore")
        _git(root, "commit", "-qm", "first")
    return Path(root)


@pytest.fixture
def repo(tmp_path):
    return _project(tmp_path, git=True)


@pytest.fixture
def plain(tmp_path):
    return _project(tmp_path, git=False)


def _grep(root):
    out, failed = tools.run_tool("grep", {"pattern": "needle"}, root=root)
    assert not failed, out
    return out


def _listed(root):
    out, failed = tools.run_tool("list_files", {}, root=root)
    assert not failed, out
    return out.replace("\\", "/")


def _completion(root):
    commands._project_files.cache_clear()
    try:
        return commands._project_files(str(root))
    finally:
        commands._project_files.cache_clear()


# --- in a git work tree ----------------------------------------------------------------

def test_grep_skips_gitignored_files(repo):
    out = _grep(repo)
    assert "service.py" in out and "notes_new.py" in out
    assert "build_out" not in out and "debug.log" not in out


def test_list_files_skips_gitignored_files(repo):
    out = _listed(repo)
    assert "src/service.py" in out and "notes_new.py" in out
    assert "build_out" not in out and "debug.log" not in out


def test_list_files_of_a_subdirectory_still_works(repo):
    out, failed = tools.run_tool("list_files", {"path": "src"}, root=repo)
    assert not failed and "service.py" in out


def test_mention_completion_skips_gitignored_files(repo):
    files = _completion(repo)
    assert "src/service.py" in files and "notes_new.py" in files
    assert not any(f.startswith("build_out/") for f in files) and "debug.log" not in files


def test_affected_tests_skip_gitignored_tests(repo):
    assert affected_tests.find_affected_tests(["src/service.py"], root=repo) == ["tests/test_service.py"]


# --- --add-dir: read-only, so git is never run there --------------------------------------

def test_no_git_runs_in_an_added_directory(repo, tmp_path_factory, monkeypatch):
    # An --add-dir directory is granted for reading only. Running git there would read
    # that repository's own config, which can name commands git runs; the project's own
    # git already runs at startup, the added directory's must not (#325 review).
    from llm_light import config, skeleton
    shared = _project(tmp_path_factory.mktemp("shared"), git=True).resolve()
    monkeypatch.setattr(config, "EXTRA_DIRS", [str(shared)])
    seen = []
    real = skeleton._git_listed_files
    monkeypatch.setattr(skeleton, "_git_listed_files",
                        lambda cwd=None: seen.append(Path(cwd).resolve() if cwd else None) or real(cwd))
    out, failed = tools.run_tool("grep", {"pattern": "needle", "glob": shared.as_posix() + "/*"}, root=repo)
    assert not failed and "build_out" in out          # walked as before, nothing filtered
    out, failed = tools.run_tool("list_files", {"path": shared.as_posix()}, root=repo)
    assert not failed and "build_out" in out.replace("\\", "/")
    assert shared not in seen


def test_listing_the_project_runs_no_fsmonitor_command(repo):
    # core.fsmonitor names a program git runs to learn what changed; listing files must
    # not run one from the repository's config.
    from llm_light import skeleton
    marker = repo / "fsmonitor-ran"
    script = repo / "fsmon.sh"
    script.write_text(f"#!/bin/sh\necho ran > '{marker.as_posix()}'\n", encoding="utf-8")
    script.chmod(0o755)
    _git(repo, "config", "core.fsmonitor", script.as_posix())
    assert skeleton.git_visible(repo) is not None
    assert not marker.exists()


# --- outside git: nothing to honour, nothing lost ---------------------------------------

def test_outside_git_grep_and_list_files_see_everything(plain):
    assert "build_out" in _grep(plain) and "debug.log" in _grep(plain)
    assert "build_out/gen.py" in _listed(plain)


def test_outside_git_completion_and_affected_tests_see_everything(plain):
    assert "build_out/gen.py" in _completion(plain)
    assert affected_tests.find_affected_tests(["src/service.py"], root=plain) == [
        "build_out/test_gen.py", "tests/test_service.py"]


def test_affected_tests_never_descend_into_skipped_folders(plain):
    _write(plain, "node_modules/pkg/test_vendored.py", "from src import service\n")
    assert affected_tests.find_affected_tests(["src/service.py"], root=plain) == [
        "build_out/test_gen.py", "tests/test_service.py"]
