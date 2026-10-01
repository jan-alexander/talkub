"""Colour for unified diffs shown on a terminal.

`colorize` runs `delta --color-only` (word-level highlighting, the user's own delta theme) when
delta is installed, and falls back to a built-in red/green colouring otherwise. `wanted` decides
whether colour is appropriate at all: only on a terminal, and only unless `NO_COLOR` is set
(https://no-color.org). Callers pass redacted text; delta is just another child process and must
never see secrets the terminal would not."""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from typing import Any

from talkub.core.runner import Runner

_RESET = "\x1b[0m"
_BOLD = "\x1b[1m"
_RED = "\x1b[31m"
_GREEN = "\x1b[32m"
_CYAN = "\x1b[36m"


def wanted(stream: Any, environ: Mapping[str, str]) -> bool:
    """True when `stream` is a terminal and NO_COLOR is unset or empty."""
    isatty = getattr(stream, "isatty", None)
    if isatty is None or not isatty():
        return False
    return not environ.get("NO_COLOR")


def colorize(runner: Runner, text: str) -> str:
    """`text` with ANSI colours: via `delta --color-only` when delta is on PATH and succeeds,
    otherwise `colorize_builtin`. Empty text stays empty (no child process)."""
    if not text:
        return text
    if shutil.which("delta") is not None:
        result = runner.run(["delta", "--color-only"], input=text.encode(), check=False)
        if result.returncode == 0:
            return result.stdout.decode(errors="replace")
    return colorize_builtin(text)


def colorize_builtin(text: str) -> str:
    """Red for removed lines, green for added ones, bold file headers, cyan hunk headers;
    everything else (context, talosctl's own summary lines) is left as it is."""
    return "\n".join(_color_line(line) for line in text.split("\n"))


def _color_line(line: str) -> str:
    if line.startswith(("--- ", "+++ ")):
        return f"{_BOLD}{line}{_RESET}"
    if line.startswith("@@"):
        return f"{_CYAN}{line}{_RESET}"
    if line.startswith("-"):
        return f"{_RED}{line}{_RESET}"
    if line.startswith("+"):
        return f"{_GREEN}{line}{_RESET}"
    return line
