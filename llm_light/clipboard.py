"""Paste an image from the clipboard into the prompt (alt+v, or ctrl+v where the terminal
passes it on).

The image is saved in the project's cache directory, which git ignores (config.
ensure_cache_dir) and the skeleton skips, as `.llm_light_cache/pastes/paste-<time>.png`,
and the prompt gets an @mention of it, so commands.attach_images() checks and sends it like
any @image. No image library is needed: each platform's own clipboard program writes the
PNG. The target path reaches that program through an environment variable, never inside
a script, so no character in the project's path can change the command.
"""

import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

from llm_light import config

TARGET_VAR = "LLM_LIGHT_PASTE_TARGET"
TIMEOUT_SECS = 15
KEEP = 20  # pasted images kept per project; older ones are removed

_WINDOWS_SCRIPT = (
    "Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
    "$image = [System.Windows.Forms.Clipboard]::GetImage(); "
    "if ($null -eq $image) { exit 3 }; "
    "$image.Save($env:" + TARGET_VAR + ", [System.Drawing.Imaging.ImageFormat]::Png)"
)
_MAC_SCRIPT = [
    f'set target to system attribute "{TARGET_VAR}"',
    "set f to open for access (POSIX file target) with write permission",
    "write (the clipboard as «class PNGf») to f",
    "close access f",
]


def grab_command(platform: str):
    """(argv, writes_to_stdout) for the program that reads this platform's clipboard
    image, or None when there is none."""
    if platform == "win32":
        # The Windows PowerShell that ships with the OS, by its full path. Clipboard
        # access needs a single-threaded apartment (-STA).
        powershell = os.path.join(os.environ.get("SystemRoot") or r"C:\Windows",
                                  "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        return [powershell, "-NoProfile", "-NonInteractive", "-STA", "-Command", _WINDOWS_SCRIPT], False
    if platform == "darwin":
        argv = ["/usr/bin/osascript"]
        for line in _MAC_SCRIPT:
            argv += ["-e", line]
        return argv, False
    wl_paste, xclip = shutil.which("wl-paste"), shutil.which("xclip")
    if wl_paste and os.environ.get("WAYLAND_DISPLAY"):
        return [wl_paste, "--no-newline", "--type", "image/png"], True
    if xclip:
        return [xclip, "-selection", "clipboard", "-target", "image/png", "-out"], True
    return None


def _prune(folder) -> None:
    """Remove all but the newest KEEP pastes. Only regular files: a link is left alone,
    since what it points at is not ours to delete."""
    pastes = [p for p in folder.glob("paste-*.png") if not p.is_symlink() and p.is_file()]
    pastes.sort(key=lambda p: (p.stat().st_mtime_ns, p.name))
    for old in pastes[:-KEEP]:
        try:
            old.unlink()
        except OSError:
            pass


def paste_image():
    """(project-relative path, None) for the saved image, or (None, what went wrong)."""
    from llm_light.commands import IMAGE_MAX_BYTES, _image_type
    command = grab_command(sys.platform)
    if command is None:
        return None, "No clipboard program found: install wl-paste (Wayland) or xclip (X11)."
    argv, to_stdout = command
    folder = config.CACHE_DIR / "pastes"
    # The cache directory comes with the checkout, so a repo could ship it, or its pastes
    # folder, as a link elsewhere: a paste would then land, and pruning delete, outside it.
    if config.CACHE_DIR.is_symlink() or folder.is_symlink():
        return None, f"{folder.as_posix()} is a link, so nothing is saved there."
    config.ensure_cache_dir()
    folder.mkdir(exist_ok=True)
    if not folder.resolve().is_relative_to(Path.cwd().resolve()):
        return None, f"{folder.as_posix()} is outside the project, so nothing is saved there."
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = folder / f"paste-{stamp}-{secrets.token_hex(3)}.png"  # two pastes in one second must not collide
    env = dict(os.environ, **{TARGET_VAR: str(target.resolve())})
    try:
        done = subprocess.run(argv, env=env, capture_output=to_stdout, stdin=subprocess.DEVNULL,
                              timeout=TIMEOUT_SECS, **({} if to_stdout else {"stdout": subprocess.DEVNULL,
                                                                             "stderr": subprocess.DEVNULL}))
    except subprocess.TimeoutExpired:
        target.unlink(missing_ok=True)
        return None, f"{os.path.basename(argv[0])} did not answer within {TIMEOUT_SECS} s."
    except OSError as e:
        return None, f"Could not run {argv[0]}: {e.strerror or e}."
    if to_stdout and done.returncode == 0 and done.stdout:
        target.write_bytes(done.stdout)
    raw = target.read_bytes() if target.is_file() else b""
    if done.returncode != 0 or _image_type(raw) is None:
        target.unlink(missing_ok=True)
        return None, "No image on the clipboard."
    if len(raw) > IMAGE_MAX_BYTES:
        target.unlink(missing_ok=True)
        return None, (f"The clipboard image is {len(raw) / 1_048_576:.1f} MB, over the "
                      f"{IMAGE_MAX_BYTES // 1_048_576} MB limit.")
    _prune(folder)
    return target.as_posix(), None
