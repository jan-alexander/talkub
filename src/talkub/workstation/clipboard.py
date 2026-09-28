"""Clipboard copy, plus scheduling the detached clearer.

The secret value is only ever handed to a child on stdin (never in argv, env, or a file). The
clearer (`talkub.workstation.clipboard_clear`) is told only a SHA-256 digest of the value, so that even that
detached, longer-lived process never holds the plaintext.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from dataclasses import dataclass

from talkub.core.errors import CommandFailed, ValidationError
from talkub.core.runner import Runner

CLEAR_SECONDS_ENV = "TALKUB_CLIPBOARD_CLEAR_SECONDS"
# Hidden first argument that makes a compiled talkub binary run the clearer (see cli.main.main).
CLEAR_COMMAND = "__clipboard-clear"
DEFAULT_CLEAR_SECONDS = 45.0


def clear_seconds() -> float:
    """Delay before the clearer empties the clipboard (env TALKUB_CLIPBOARD_CLEAR_SECONDS, default 45;
    an invalid or negative value falls back to the default)."""
    try:
        seconds = float(os.environ.get(CLEAR_SECONDS_ENV, DEFAULT_CLEAR_SECONDS))
    except ValueError:
        return DEFAULT_CLEAR_SECONDS
    return seconds if 0 <= seconds < float("inf") else DEFAULT_CLEAR_SECONDS


@dataclass(frozen=True)
class ClipboardTool:
    copy: list[str]
    paste: list[str]
    clear: list[str]


# In order of preference: macOS, Wayland, X11.
_TOOLS = (
    ClipboardTool(copy=["pbcopy"], paste=["pbpaste"], clear=["pbcopy"]),
    # wl-paste appends a trailing newline by default; -n suppresses it so the clearer's hash of
    # the pasted content matches the hash of what was copied. wl-copy has a dedicated --clear.
    ClipboardTool(copy=["wl-copy"], paste=["wl-paste", "-n"], clear=["wl-copy", "--clear"]),
    ClipboardTool(
        copy=["xclip", "-selection", "clipboard"],
        paste=["xclip", "-selection", "clipboard", "-o"],
        clear=["xclip", "-selection", "clipboard"],
    ),
)


def tool() -> ClipboardTool | None:
    """The first available clipboard tool, or None if none is on PATH."""
    for candidate in _TOOLS:
        if shutil.which(candidate.copy[0]) is not None:
            return candidate
    return None


def copy(runner: Runner, value: bytes) -> ClipboardTool:
    """Copy `value` to the clipboard via stdin. Raises ValidationError if no tool is found.

    Uses `Runner.feed` (not `run`): copy tools often fork a background process that holds the
    selection and inherits stdout/stderr, and capturing with a pipe would hang on it.
    """
    found = tool()
    if found is None:
        raise ValidationError(
            "no clipboard tool found",
            hint="install pbcopy/wl-copy/xclip or use --print",
        )
    code = runner.feed(found.copy, input=value)
    if code != 0:
        raise CommandFailed("clipboard copy failed", hint="use --print")
    return found


def clearer_argv() -> list[str]:
    """How to start the clearer: `python -I -m ...`, or the talkub binary itself when compiled.

    Nuitka defines `__compiled__` in every compiled module. There `sys.executable` names a
    `python3` that does not exist; `sys.argv[0]` is the absolute path of the onefile binary.
    """
    if "__compiled__" in globals():
        return [sys.argv[0], CLEAR_COMMAND]
    # -I: isolated mode, so the clearer's sys.path does not depend on the caller's cwd.
    return [sys.executable, "-I", "-m", "talkub.workstation.clipboard_clear"]


def schedule_clear(runner: Runner, value: bytes) -> None:
    """Spawn the detached clearer, handing it only the SHA-256 digest of `value` on stdin."""
    digest = hashlib.sha256(value).hexdigest().encode()
    runner.spawn_detached(clearer_argv(), input=digest)
