"""The claude_light compatibility names are gone (#119).

The rename to llm_light (#121) kept the old names working for a release: the
claude-light console script, claude_light.py, CLAUDE_LIGHT_* settings and the
~/.claude_light_* home files. They are no longer read. Because a setting that
silently stops applying can matter (CLAUDE_LIGHT_PROXY would stop routing the
traffic through llm-fw), startup names every legacy setting or file it finds
and the new name to use, without honouring it.
"""

import io
import os
import subprocess
import sys
import tomllib
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from llm_light import config

REPO = Path(__file__).resolve().parents[2]


def _env_without_settings():
    return {k: v for k, v in os.environ.items()
            if not k.startswith(("LLM_LIGHT_", "CLAUDE_LIGHT_"))}


def test_env_get_ignores_the_legacy_name(monkeypatch):
    monkeypatch.delenv("LLM_LIGHT_TEST_KNOB", raising=False)
    monkeypatch.setenv("CLAUDE_LIGHT_TEST_KNOB", "old")
    assert config.env_get("TEST_KNOB", "fallback") == "fallback"


def test_env_get_reads_the_new_name(monkeypatch):
    monkeypatch.setenv("LLM_LIGHT_TEST_KNOB", "new")
    assert config.env_get("TEST_KNOB") == "new"


def test_legacy_env_var_no_longer_changes_a_constant():
    # config reads the environment at import, so check a fresh interpreter.
    env = _env_without_settings()
    env.update({"CLAUDE_LIGHT_ROUTE_ONCE": "0", "CLAUDE_LIGHT_WORKTREE_BOOST": "2.5"})
    code = "from llm_light import config as c; print(c.ROUTE_ONCE, c.WORKTREE_BOOST)"
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env,
                         capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1].split() == ["True", "1.0"]


def test_legacy_automation_token_is_not_read(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    (tmp_path / ".claude_light_automation_token").write_text("legacy-token\n", encoding="utf-8")
    assert config._get_pro_token() != "legacy-token"
    assert not (tmp_path / ".llm_light_automation_token").exists()  # nothing copied


def test_the_migration_helper_is_gone():
    assert not hasattr(config, "_migrate_path")


# --- the startup notice ------------------------------------------------------

def test_notice_names_each_legacy_setting_and_its_new_name(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("CLAUDE_LIGHT_PROXY", "http://127.0.0.1:8080")
    notices = config.legacy_notices()
    assert any("CLAUDE_LIGHT_PROXY" in n and "LLM_LIGHT_PROXY" in n and "no longer read" in n
               for n in notices)


def test_notice_names_a_legacy_home_file_only_while_the_new_one_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    for var in [v for v in os.environ if v.startswith("CLAUDE_LIGHT_")]:
        monkeypatch.delenv(var)
    (tmp_path / ".claude_light_stats.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".claude_light_sessions").mkdir()
    notices = config.legacy_notices()
    assert len(notices) == 2
    assert any(".claude_light_stats.json" in n and ".llm_light_stats.json" in n for n in notices)
    (tmp_path / ".llm_light_stats.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".llm_light_sessions").mkdir()
    assert config.legacy_notices() == []


def test_main_prints_the_notice_at_startup(monkeypatch, tmp_path):
    from llm_light import main as main_mod, providers, state
    from llm_light.providers import base

    class _Bare(base.Provider):
        name = "bare"

        def model_label(self, routed_model):
            return "m"

        def require_auth(self):
            pass

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("CLAUDE_LIGHT_ROUTE_ONCE", "0")
    monkeypatch.setattr(sys, "argv", ["llm_light", "hello"])
    monkeypatch.setattr(config, "ALLOWED_COMMANDS", [])
    monkeypatch.setattr(providers, "select", lambda name: _Bare())
    monkeypatch.setattr(state, "provider", _Bare(), raising=False)
    monkeypatch.setattr(main_mod, "_start_llmlingua_load", lambda: None)
    monkeypatch.setattr(main_mod, "one_shot", lambda *a, **k: None)
    err = io.StringIO()
    with redirect_stdout(io.StringIO()), redirect_stderr(err):
        try:
            main_mod.main()
        except SystemExit:
            pass
    assert "CLAUDE_LIGHT_ROUTE_ONCE is no longer read" in err.getvalue()


# --- the old entry points ----------------------------------------------------

def test_only_the_llm_light_console_script_remains():
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["scripts"] == {"llm-light": "llm_light.__main__:main"}


def test_the_legacy_entry_script_is_gone():
    assert not (REPO / "claude_light.py").exists()
