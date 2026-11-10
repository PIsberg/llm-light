"""Type keys into another process's Windows console, as a user would: python console_keys.py
<pid> <text>. "\\x1b" in the text is Esc. Run detached: a process can only attach to
another console after leaving its own."""

import ctypes
import os
import signal
import sys
from ctypes import wintypes

KEY_EVENT = 0x0001


class _KeyEvent(ctypes.Structure):
    _fields_ = [("bKeyDown", wintypes.BOOL), ("wRepeatCount", wintypes.WORD),
                ("wVirtualKeyCode", wintypes.WORD), ("wVirtualScanCode", wintypes.WORD),
                ("uChar", wintypes.WCHAR), ("dwControlKeyState", wintypes.DWORD)]


class _Event(ctypes.Union):
    _fields_ = [("KeyEvent", _KeyEvent)]


class _InputRecord(ctypes.Structure):
    _fields_ = [("EventType", wintypes.WORD), ("Event", _Event)]


def main():
    pid, text = int(sys.argv[1]), sys.argv[2].encode().decode("unicode_escape")
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32")
    # The Esc typed here makes the target send Ctrl+C to its console, which this
    # process is about to join: ignore it, or it ends this process before it exits cleanly.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    k32.FreeConsole()
    # os._exit: once detached, flushing stdout at a normal exit fails (exit status 120).
    if not k32.AttachConsole(pid):
        os._exit(3)
    handle = k32.CreateFileW("CONIN$", 0xC0000000, 3, None, 3, 0, None)
    records = []
    for ch in text:
        vk = 0x1B if ch == "\x1b" else (user32.VkKeyScanW(ord(ch)) & 0xFF)
        for down in (True, False):
            rec = _InputRecord()
            rec.EventType = KEY_EVENT
            rec.Event.KeyEvent = _KeyEvent(down, 1, vk, 0, ch, 0)
            records.append(rec)
    array = (_InputRecord * len(records))(*records)
    written = wintypes.DWORD()
    if not k32.WriteConsoleInputW(handle, array, len(records), ctypes.byref(written)):
        os._exit(4)
    k32.FreeConsole()
    os._exit(0)


if __name__ == "__main__":
    main()
