"""Clipboard copy, plus scheduling the detached clearer.

The secret value is only ever handed to a child on stdin (never in argv, env, or a file). The
clearer (`talkub.clipboard_clear`) is told only a SHA-256 digest of the value, so that even that
detached, longer-lived process never holds the plaintext.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
from dataclasses import dataclass

from talkub.errors import ValidationError
from talkub.runner import Runner


@dataclass(frozen=True)
class ClipboardTool:
    copy: list[str]
    paste: list[str]


# In order of preference: macOS, Wayland, X11.
_TOOLS = (
    ClipboardTool(copy=["pbcopy"], paste=["pbpaste"]),
    ClipboardTool(copy=["wl-copy"], paste=["wl-paste"]),
    ClipboardTool(copy=["xclip", "-selection", "clipboard"], paste=["xclip", "-selection", "clipboard", "-o"]),
)


def tool() -> ClipboardTool | None:
    """The first available clipboard tool, or None if none is on PATH."""
    for candidate in _TOOLS:
        if shutil.which(candidate.copy[0]) is not None:
            return candidate
    return None


def copy(runner: Runner, value: bytes) -> ClipboardTool:
    """Copy `value` to the clipboard via stdin. Raises ValidationError if no tool is found."""
    found = tool()
    if found is None:
        raise ValidationError(
            "no clipboard tool found", hint="install pbcopy/wl-copy/xclip or use --print"
        )
    runner.run(found.copy, input=value)
    return found


def schedule_clear(runner: Runner, value: bytes) -> None:
    """Spawn the detached clearer, handing it only the SHA-256 digest of `value` on stdin."""
    digest = hashlib.sha256(value).hexdigest().encode()
    runner.spawn_detached([sys.executable, "-m", "talkub.clipboard_clear"], input=digest)
