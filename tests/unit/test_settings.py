"""~/.llm_light/settings.json and /config; themes, the bell, -C, --append-system-prompt, completions."""

import json
import os
import shutil
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

import llm_light.state as state
from llm_light import completions, config, llm, permissions, settings, ui, user_shell
from llm_light import main as m
from llm_light.providers import claude, codex


@pytest.fixture(autouse=True)
def _settings_file(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(settings, "_file", lambda: path)
    saved = (state.vim_mode, state.theme, state.notify, state.pinned_effort, state.pinned_model)
    yield path
    state.vim_mode, state.theme, state.notify, state.pinned_effort, state.pinned_model = saved
    state.extra_system = ""
    permissions.set_mode(permissions.DEFAULT)


# --- loading: a damaged or hand-edited file grants nothing ---------------------------

def test_no_file_is_no_settings():
    assert settings.load() == {}


@pytest.mark.parametrize("content", ["not json", "[1, 2]", '"text"'])
def test_a_damaged_file_is_ignored(_settings_file, content):
    _settings_file.write_text(content, encoding="utf-8")
    assert settings.load() == {}


def test_only_known_keys_and_values_survive(_settings_file):
    _settings_file.write_text(json.dumps({"vim": "on", "theme": "purple", "other": 1, "notify": 5}), encoding="utf-8")
    assert settings.load() == {"vim": "on"}


def test_the_file_cannot_turn_off_permission_prompts(_settings_file):
    """Bypass takes a command-line flag every time, so nothing can leave it on quietly."""
    _settings_file.write_text(json.dumps({"permission_mode": "bypassPermissions"}), encoding="utf-8")
    assert settings.load() == {}
    assert "bypassPermissions" not in settings.config_command("permission_mode bypassPermissions").split(":")[0]
    assert settings.load() == {}


# --- /config ------------------------------------------------------------------------

def test_config_lists_settings_and_where_they_live(_settings_file):
    text = settings.config_command("")
    assert str(_settings_file) in text
    for key in ("vim", "theme", "notify", "permission_mode", "model", "effort"):
        assert key in text


def test_config_saves_and_applies_at_once(_settings_file):
    line = settings.config_command("vim on")
    assert "vim" in line
    assert state.vim_mode is True
    assert json.loads(_settings_file.read_text(encoding="utf-8")) == {"vim": "on"}


def test_config_keeps_the_other_settings(_settings_file):
    settings.config_command("vim on")
    settings.config_command("theme light")
    assert json.loads(_settings_file.read_text(encoding="utf-8")) == {"vim": "on", "theme": "light"}


def test_config_refuses_an_unknown_key_or_value(_settings_file):
    assert "Unknown" in settings.config_command("nope on")
    assert "theme" in settings.config_command("theme purple")
    assert not _settings_file.exists()


def test_config_reports_a_file_it_cannot_write(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "_file", lambda: tmp_path)  # a directory
    assert "Could not save" in settings.config_command("vim on")


def test_config_sets_the_permission_mode_now():
    settings.config_command("permission_mode plan")
    assert permissions.mode() == permissions.PLAN


# --- applied at startup; a flag beats a setting --------------------------------------

def _main(monkeypatch, argv, tty=True):
    fake = MagicMock()
    fake.has.return_value = True
    monkeypatch.setattr(sys, "argv", ["llm_light"] + argv)
    monkeypatch.setattr(m.providers, "select", MagicMock(return_value=fake))
    monkeypatch.setattr(m, "one_shot", MagicMock())
    start_chat = MagicMock()
    monkeypatch.setattr(m, "start_chat", start_chat)
    monkeypatch.setattr(m, "_start_llmlingua_load", lambda: None)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: tty)
    m.main()
    return fake


def test_settings_apply_at_startup(_settings_file, monkeypatch):
    _settings_file.write_text(json.dumps({"vim": "on", "theme": "light", "notify": "bell",
                                          "permission_mode": "acceptEdits", "model": "haiku",
                                          "effort": "low"}), encoding="utf-8")
    _main(monkeypatch, [])
    assert state.vim_mode is True and state.theme == "light" and state.notify == "bell"
    assert permissions.mode() == permissions.ACCEPT_EDITS
    assert state.pinned_model == config.MODEL_HAIKU and state.pinned_effort == "low"


def test_a_flag_beats_a_setting(_settings_file, monkeypatch):
    _settings_file.write_text(json.dumps({"permission_mode": "acceptEdits", "model": "haiku"}), encoding="utf-8")
    _main(monkeypatch, ["--permission-mode", "plan", "--model", "opus"])
    assert permissions.mode() == permissions.PLAN
    assert state.pinned_model == config.MODEL_OPUS


def test_a_saved_model_is_dropped_quietly_where_the_provider_picks(_settings_file, monkeypatch, capsys):
    state.pinned_model = None
    state.pinned_effort = None
    _settings_file.write_text(json.dumps({"model": "haiku", "effort": "low"}), encoding="utf-8")
    fake = codex.PROVIDER
    monkeypatch.setattr(sys, "argv", ["llm_light"])
    monkeypatch.setattr(m.providers, "select", MagicMock(return_value=fake))
    monkeypatch.setattr(fake, "require_auth", lambda: None)
    monkeypatch.setattr(m, "start_chat", MagicMock())
    monkeypatch.setattr(m, "_start_llmlingua_load", lambda: None)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    m.main()
    assert state.pinned_model is None and state.pinned_effort is None
    assert "ignored" not in capsys.readouterr().err  # a flag says so; a setting is not an error


# --- theme --------------------------------------------------------------------------

def test_the_dark_theme_stays_the_default():
    state.theme = "dark"
    assert ui.code_theme() == "monokai"


def test_the_light_theme_changes_code_and_diffs():
    state.theme = "light"
    assert ui.code_theme() == "ansi_light"
    assert ui._reply_markdown("```python\nx = 1\n```").code_theme == "ansi_light"


# --- the bell -----------------------------------------------------------------------

class _Tty:
    def __init__(self):
        self.written = ""

    def isatty(self):
        return True

    def write(self, text):
        self.written += text

    def flush(self):
        pass


@pytest.mark.parametrize("mode, seconds, rings", [("bell", 60, True), ("bell", 1, False),
                                                  ("off", 60, False)])
def test_the_bell_rings_only_after_a_long_turn_when_asked(mode, seconds, rings):
    state.notify = mode
    tty = _Tty()
    with patch.object(sys, "stdout", tty):
        ui.turn_finished(seconds)
    assert ("\a" in tty.written) is rings


def test_no_bell_into_a_file(capsys):
    state.notify = "bell"
    ui.turn_finished(60)
    assert "\a" not in capsys.readouterr().out


# --- -C -----------------------------------------------------------------------------

def test_cd_runs_in_that_directory(monkeypatch, tmp_path):
    (tmp_path / "other").mkdir()
    monkeypatch.chdir(tmp_path)
    _main(monkeypatch, ["-C", "other", "-p", "q"])
    assert os.path.samefile(os.getcwd(), tmp_path / "other")


def test_cd_to_nowhere_is_a_usage_error(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as stopped:
        _main(monkeypatch, ["-C", "missing", "-p", "q"])
    assert stopped.value.code == 2


# --- --append-system-prompt ----------------------------------------------------------

def test_by_default_the_system_blocks_are_what_they_were():
    state.extra_system = ""
    blocks = llm._build_system_blocks("SKELETON")
    assert blocks[0]["text"] == config.SYSTEM_PROMPT
    assert blocks[1]["text"] == "SKELETON"


def test_appended_text_goes_ahead_of_the_cache_breakpoint():
    state.extra_system = "Answer in French."
    blocks = llm._build_system_blocks("SKELETON")
    assert "Answer in French." in blocks[0]["text"]
    assert "cache_control" in blocks[-1]


def test_the_flag_sets_it(monkeypatch):
    _main(monkeypatch, ["--append-system-prompt", "Answer in French.", "-p", "q"])
    assert state.extra_system == "Answer in French."


# --- completions --------------------------------------------------------------------

def _parser():
    return m._build_parser()


def test_every_flag_is_in_every_script():
    for shell in completions.SHELLS:
        script = completions.script(shell, _parser())
        for flag in ("--provider", "--output-format", "--doctor", "--permission-mode"):
            assert flag[2:] in script, (shell, flag)


def test_choices_complete_as_values():
    script = completions.script("bash", _parser())
    assert "acceptEdits" in script and "stream-json" in script


# On Windows `bash` on PATH is usually WSL's, which cannot see a Windows temp path.
_BASH = user_shell.git_bash() if os.name == "nt" else shutil.which("bash")


@pytest.mark.skipif(_BASH is None, reason="needs bash")
def test_the_bash_script_parses_and_completes(tmp_path):
    path = tmp_path / "c.bash"
    path.write_text(completions.script("bash", _parser()), encoding="utf-8", newline="\n")
    probe = (f'source "{path.as_posix()}"; COMP_WORDS=(llm-light --prov); COMP_CWORD=1; '
             '_llm_light_complete; echo "${COMPREPLY[@]}"; '
             'COMP_WORDS=(llm-light --provider co); COMP_CWORD=2; '
             '_llm_light_complete; echo "${COMPREPLY[@]}"')
    out = subprocess.run([_BASH, "-c", probe], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["--provider", "codex"]


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell")
def test_the_powershell_script_parses():
    script = completions.script("powershell", _parser())
    code = "[ScriptBlock]::Create([Console]::In.ReadToEnd()) | Out-Null; 'parsed'"
    out = subprocess.run(["powershell", "-NoProfile", "-Command", code], input=script,
                         capture_output=True, text=True, timeout=60)
    assert "parsed" in out.stdout, out.stderr


def test_the_flag_prints_a_script_without_needing_a_login(monkeypatch, capsys):
    provider = MagicMock()
    provider.require_auth.side_effect = AssertionError("needed a login")
    monkeypatch.setattr(sys, "argv", ["llm_light", "--completion", "fish"])
    monkeypatch.setattr(m.providers, "select", MagicMock(return_value=provider))
    with pytest.raises(SystemExit) as stopped:
        m.main()
    assert stopped.value.code == 0
    assert "complete -c llm-light" in capsys.readouterr().out
