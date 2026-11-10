"""alt+v (and ctrl+v where the terminal passes it on) pastes an image from the clipboard.

The image is saved in the project's cache directory, which git and the skeleton ignore,
and an @mention of it goes in the prompt, so it is attached like any @image.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from llm_light import clipboard, commands
from llm_light import main as m

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project directory, with the grabber pinned so the result does not depend on
    which clipboard program the machine running the tests has (CI's Linux has none)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(clipboard, "grab_command", lambda platform: (["grabber"], False))
    return tmp_path


def _grabbed(data=PNG, code=0):
    """A fake subprocess.run for the grabber: writes `data` where the command was told."""
    def run(argv, **kwargs):
        target = kwargs["env"][clipboard.TARGET_VAR]
        if data is not None and code == 0:
            if kwargs.get("capture_output"):
                return subprocess.CompletedProcess(argv, code, stdout=data, stderr=b"")
            Path(target).write_bytes(data)
        return subprocess.CompletedProcess(argv, code, stdout=b"", stderr=b"")
    return run


# --- which program reads the clipboard ------------------------------------------------------

def test_windows_uses_powershell_in_sta_and_takes_the_path_from_the_environment():
    argv, to_stdout = clipboard.grab_command("win32")
    assert argv[0].lower().replace("/", "\\").endswith(
        "\\system32\\windowspowershell\\v1.0\\powershell.exe")
    assert "-STA" in argv
    # The path never goes into the script, so no character in it can change the command.
    assert "$env:" + clipboard.TARGET_VAR in argv[-1]
    assert not to_stdout


def test_macos_uses_osascript():
    argv, to_stdout = clipboard.grab_command("darwin")
    assert argv[0] == "/usr/bin/osascript" and clipboard.TARGET_VAR in " ".join(argv)
    assert not to_stdout


def test_linux_prefers_wl_paste_under_wayland(monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    with patch.object(clipboard.shutil, "which", side_effect=lambda n: f"/usr/bin/{n}"):
        argv, to_stdout = clipboard.grab_command("linux")
    assert argv[:3] == ["/usr/bin/wl-paste", "--no-newline", "--type"] and to_stdout


def test_linux_falls_back_to_xclip(monkeypatch):
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    with patch.object(clipboard.shutil, "which", side_effect=lambda n: "/usr/bin/xclip" if n == "xclip" else None):
        argv, to_stdout = clipboard.grab_command("linux")
    assert argv[0] == "/usr/bin/xclip" and "image/png" in argv and to_stdout


def test_linux_without_a_tool_says_what_to_install(monkeypatch):
    with patch.object(clipboard.shutil, "which", return_value=None):
        assert clipboard.grab_command("linux") is None
    with patch.object(clipboard, "grab_command", return_value=None):
        path, problem = clipboard.paste_image()
    assert path is None and "wl-paste" in problem and "xclip" in problem


# --- saving it ------------------------------------------------------------------------------

def test_a_pasted_image_lands_in_the_cache_and_can_be_attached(project):
    with patch.object(clipboard.subprocess, "run", side_effect=_grabbed()):
        rel, problem = clipboard.paste_image()
    assert problem is None
    assert rel.startswith(".llm_light_cache/pastes/") and rel.endswith(".png")
    assert (project / rel).read_bytes() == PNG
    assert (project / ".llm_light_cache" / ".gitignore").is_file()  # git ignores it
    images, problems = commands.attach_images(f"look at @{rel}", project)
    assert problems == [] and images[0]["media_type"] == "image/png"


def test_stdout_grabbers_are_written_to_the_file(project):
    with patch.object(clipboard, "grab_command", return_value=(["/usr/bin/xclip"], True)), \
         patch.object(clipboard.subprocess, "run", side_effect=_grabbed()):
        rel, problem = clipboard.paste_image()
    assert problem is None and (project / rel).read_bytes() == PNG


@pytest.mark.parametrize("data, code", [(None, 1), (b"", 0), (b"just text", 0)])
def test_no_image_on_the_clipboard(project, data, code):
    with patch.object(clipboard.subprocess, "run", side_effect=_grabbed(data, code)):
        rel, problem = clipboard.paste_image()
    assert rel is None and "no image" in problem.lower()
    assert list((project / ".llm_light_cache" / "pastes").glob("*.png")) == []  # nothing left behind


def test_too_large(project):
    big = PNG + b"\0" * commands.IMAGE_MAX_BYTES
    with patch.object(clipboard.subprocess, "run", side_effect=_grabbed(big)):
        rel, problem = clipboard.paste_image()
    assert rel is None and "MB" in problem


def test_a_grabber_that_hangs_is_stopped(project):
    with patch.object(clipboard.subprocess, "run",
                      side_effect=subprocess.TimeoutExpired("powershell", clipboard.TIMEOUT_SECS)):
        rel, problem = clipboard.paste_image()
    assert rel is None and "did not answer" in problem


def test_old_pastes_are_pruned(project):
    with patch.object(clipboard.subprocess, "run", side_effect=_grabbed()):
        for _ in range(clipboard.KEEP + 3):
            clipboard.paste_image()
    assert len(list((project / ".llm_light_cache" / "pastes").glob("*.png"))) == clipboard.KEEP


# --- the key ----------------------------------------------------------------------------------

def _prompt(keys):
    with create_pipe_input() as inp:
        inp.send_text(keys)
        return PromptSession(input=inp, output=DummyOutput(), history=InMemoryHistory(),
                             key_bindings=m._prompt_bindings()).prompt()


@pytest.mark.parametrize("key", ["\x1bv", "\x16"])  # alt+v, ctrl+v
def test_the_key_puts_a_mention_in_the_prompt(key):
    with patch.object(clipboard, "paste_image", return_value=(".llm_light_cache/pastes/p.png", None)):
        assert _prompt("what is this " + key + "\r") == "what is this @.llm_light_cache/pastes/p.png "


def test_without_an_image_the_prompt_is_unchanged():
    with patch.object(clipboard, "paste_image", return_value=(None, "No image on the clipboard.")):
        assert _prompt("draft\x1bv\r") == "draft"


def test_help_names_the_key():
    assert any("alt+v" in key for key, _ in m._KEYS)


# --- a cache directory the checkout controls ----------------------------------------------

@pytest.mark.parametrize("linked", [".llm_light_cache", "pastes"])
def test_a_linked_cache_or_pastes_folder_is_refused(project, linked):
    # A cloned repo can ship either as a link elsewhere. Simulated, since creating a real
    # link needs a privilege on Windows.
    real = Path.is_symlink
    with patch.object(Path, "is_symlink", lambda self: self.name == linked or real(self)), \
         patch.object(clipboard.subprocess, "run") as run:
        rel, problem = clipboard.paste_image()
    assert rel is None and "link" in problem
    run.assert_not_called()


def test_pruning_leaves_links_alone(project):
    real = Path.is_symlink
    with patch.object(clipboard.subprocess, "run", side_effect=_grabbed()):
        clipboard.paste_image()
    kept = project / ".llm_light_cache" / "pastes" / "paste-00000000-000000-link00.png"
    kept.write_bytes(PNG)
    with patch.object(Path, "is_symlink", lambda self: self.name == kept.name or real(self)), \
         patch.object(clipboard.subprocess, "run", side_effect=_grabbed()), \
         patch.object(clipboard, "KEEP", 1):
        clipboard.paste_image()
    assert kept.exists()
    assert len(list(kept.parent.glob("paste-*.png"))) == 2  # the link and the newest paste
