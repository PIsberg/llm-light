"""The project was renamed from claude_light to llm_light. Existing installs
must keep working: old env vars, the automation token, the stats and session
files, the claude-light console script and the claude_light.py entry point."""

import os
import subprocess
import sys
import tomllib
from pathlib import Path

from llm_light import config

REPO = Path(__file__).resolve().parents[2]


def _env_without_settings():
    return {k: v for k, v in os.environ.items()
            if not k.startswith(("LLM_LIGHT_", "CLAUDE_LIGHT_"))}


def test_env_get_prefers_the_new_name(monkeypatch):
    monkeypatch.setenv("LLM_LIGHT_TEST_KNOB", "new")
    monkeypatch.setenv("CLAUDE_LIGHT_TEST_KNOB", "old")
    assert config.env_get("TEST_KNOB") == "new"


def test_env_get_falls_back_to_the_legacy_name(monkeypatch):
    monkeypatch.delenv("LLM_LIGHT_TEST_KNOB", raising=False)
    monkeypatch.setenv("CLAUDE_LIGHT_TEST_KNOB", "old")
    assert config.env_get("TEST_KNOB") == "old"


def test_env_get_returns_default_when_neither_name_is_set(monkeypatch):
    monkeypatch.delenv("LLM_LIGHT_TEST_KNOB", raising=False)
    monkeypatch.delenv("CLAUDE_LIGHT_TEST_KNOB", raising=False)
    assert config.env_get("TEST_KNOB", "fallback") == "fallback"
    assert config.env_get("TEST_KNOB") is None


def test_legacy_env_var_changes_a_constant_the_code_reads():
    # config reads the environment at import, so check a fresh interpreter.
    env = _env_without_settings()
    env.update({"CLAUDE_LIGHT_ROUTE_ONCE": "0", "CLAUDE_LIGHT_WORKTREE_BOOST": "2.5"})
    code = "from llm_light import config as c; print(c.ROUTE_ONCE, c.WORKTREE_BOOST)"
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env,
                         capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1].split() == ["False", "2.5"]


def test_legacy_automation_token_is_copied_and_read(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    (tmp_path / ".claude_light_automation_token").write_text("legacy-token\n", encoding="utf-8")
    assert config._get_pro_token() == "legacy-token"
    assert (tmp_path / ".llm_light_automation_token").read_text(encoding="utf-8") == "legacy-token\n"
    assert (tmp_path / ".claude_light_automation_token").exists()
    assert ".llm_light_automation_token" in capsys.readouterr().err


def test_new_automation_token_wins_over_the_legacy_one(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    (tmp_path / ".claude_light_automation_token").write_text("legacy-token", encoding="utf-8")
    (tmp_path / ".llm_light_automation_token").write_text("new-token", encoding="utf-8")
    assert config._get_pro_token() == "new-token"


# --- #120: the old home files are copied to the new names once -------------------

def test_migrate_path_copies_a_legacy_file_and_keeps_the_original(tmp_path, capsys):
    legacy, new = tmp_path / ".claude_light_stats.json", tmp_path / ".llm_light_stats.json"
    legacy.write_text('{"turns": 3}', encoding="utf-8")
    assert config._migrate_path(new, legacy) == new
    assert new.read_text(encoding="utf-8") == '{"turns": 3}'
    assert legacy.read_text(encoding="utf-8") == '{"turns": 3}'  # copied, so a downgrade works
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1
    assert str(legacy) in err[0] and str(new) in err[0]


def test_migrate_path_copies_a_legacy_session_folder(tmp_path, capsys):
    legacy, new = tmp_path / ".claude_light_sessions", tmp_path / ".llm_light_sessions"
    (legacy / "sub").mkdir(parents=True)
    (legacy / "a.json").write_text("A", encoding="utf-8")
    (legacy / "sub" / "b.json").write_text("B", encoding="utf-8")
    assert config._migrate_path(new, legacy) == new
    assert (new / "a.json").read_text(encoding="utf-8") == "A"
    assert (new / "sub" / "b.json").read_text(encoding="utf-8") == "B"
    assert (legacy / "a.json").exists()
    assert len(capsys.readouterr().err.strip().splitlines()) == 1


def test_migrate_path_is_idempotent_and_never_overwrites_the_new_file(tmp_path, capsys):
    legacy, new = tmp_path / "old.json", tmp_path / "new.json"
    legacy.write_text("old", encoding="utf-8")
    config._migrate_path(new, legacy)
    capsys.readouterr()
    new.write_text("changed since", encoding="utf-8")
    assert config._migrate_path(new, legacy) == new
    assert new.read_text(encoding="utf-8") == "changed since"
    assert capsys.readouterr().err == ""


def test_migrate_path_without_a_legacy_file_creates_nothing(tmp_path, capsys):
    legacy, new = tmp_path / "old.json", tmp_path / "new.json"
    assert config._migrate_path(new, legacy) == new
    assert not new.exists()
    assert capsys.readouterr().err == ""


def test_migrate_path_falls_back_to_the_legacy_file_when_the_copy_fails(monkeypatch, tmp_path):
    legacy, new = tmp_path / "old.json", tmp_path / "new.json"
    legacy.write_text("old", encoding="utf-8")

    def refuse(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(config.shutil, "copy2", refuse)
    assert config._migrate_path(new, legacy) == legacy
    assert not new.exists()


def test_migrate_path_never_copies_or_prints_from_a_background_thread(tmp_path, capsys):
    # CLAUDE.md invariant: background threads never print.
    import threading
    legacy, new = tmp_path / "old.json", tmp_path / "new.json"
    legacy.write_text("old", encoding="utf-8")
    seen = []
    t = threading.Thread(target=lambda: seen.append(config._migrate_path(new, legacy)))
    t.start()
    t.join()
    assert seen == [legacy]
    assert not new.exists()
    assert capsys.readouterr().err == ""


def test_both_console_scripts_run_the_same_main():
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = data["project"]["scripts"]
    assert scripts["llm-light"] == "llm_light.__main__:main"
    assert scripts["claude-light"] == "llm_light.__main__:main"


def test_legacy_entry_script_still_runs():
    out = subprocess.run([sys.executable, "claude_light.py", "--help"], cwd=REPO,
                         capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr
    assert "usage" in out.stdout.lower()
