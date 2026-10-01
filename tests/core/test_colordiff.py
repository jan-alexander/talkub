from __future__ import annotations

from helpers import FakeRunner

from talkub.core import colordiff
from talkub.core.runner import Runner


class _Stream:
    def __init__(self, tty: bool):
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_wanted_only_on_a_terminal():
    assert colordiff.wanted(_Stream(True), {}) is True
    assert colordiff.wanted(_Stream(False), {}) is False


def test_wanted_respects_no_color():
    assert colordiff.wanted(_Stream(True), {"NO_COLOR": "1"}) is False
    assert colordiff.wanted(_Stream(True), {"NO_COLOR": ""}) is True  # the convention: only a non-empty value


def test_wanted_survives_streams_without_isatty():
    assert colordiff.wanted(object(), {}) is False


DIFF = "Config diff:\n\n--- a\n+++ b\n@@ -1,2 +1,2 @@\n foo: 1\n-bar: 2\n+bar: 3\n"


def test_builtin_colors_added_and_removed_lines_only():
    out = colordiff.colorize_builtin(DIFF)
    lines = out.split("\n")
    assert lines[0] == "Config diff:"
    assert lines[2] == "\x1b[1m--- a\x1b[0m"
    assert lines[3] == "\x1b[1m+++ b\x1b[0m"
    assert lines[4] == "\x1b[36m@@ -1,2 +1,2 @@\x1b[0m"
    assert lines[5] == " foo: 1"
    assert lines[6] == "\x1b[31m-bar: 2\x1b[0m"
    assert lines[7] == "\x1b[32m+bar: 3\x1b[0m"
    assert out.endswith("\n")


def test_colorize_uses_delta_when_installed(spy):
    delta = spy("delta").on("--color-only", read_stdin=True, stdout="COLORED\n")
    assert colordiff.colorize(Runner(), DIFF) == "COLORED\n"
    (call,) = delta.calls
    assert call["args"] == ["--color-only"]
    assert call["stdin"] == DIFF


def test_colorize_falls_back_when_delta_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))  # no delta anywhere on PATH
    runner = FakeRunner()
    assert colordiff.colorize(runner, DIFF) == colordiff.colorize_builtin(DIFF)
    assert runner.calls == []


def test_colorize_falls_back_when_delta_fails(spy):
    spy("delta").on("--color-only", rc=1, stderr="boom\n")
    assert colordiff.colorize(Runner(), DIFF) == colordiff.colorize_builtin(DIFF)


def test_colorize_passes_empty_text_through(spy):
    delta = spy("delta")
    assert colordiff.colorize(Runner(), "") == ""
    assert delta.calls == []
