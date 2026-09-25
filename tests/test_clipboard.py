"""Clipboard copy and the detached clearer: the secret value must never reach argv/env/files."""

from __future__ import annotations

import hashlib
import io
import os
import sys
import time

import pytest

from talkub.errors import CommandFailed, ValidationError
from talkub.runner import Runner

SECRET = b"top-secret-value"


def _bin_dir_with(tmp_path, *names):
    """A PATH-able dir holding no-op executables named `names` only (no real tools reachable)."""
    directory = tmp_path / "onlybin"
    directory.mkdir()
    for name in names:
        path = directory / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    return directory


def _hide_real_tools(monkeypatch, tmp_path):
    """Restrict PATH to an empty dir (python is still found via its absolute sys.executable)."""
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))


def test_tool_prefers_pbcopy_when_several_are_on_path(spy):
    from talkub import clipboard

    spy("pbcopy")
    spy("wl-copy")
    found = clipboard.tool()
    assert found is not None
    assert found.copy == ["pbcopy"]
    assert found.paste == ["pbpaste"]


def test_tool_pins_pbcopy_argv(tmp_path, monkeypatch):
    from talkub import clipboard

    monkeypatch.setenv("PATH", str(_bin_dir_with(tmp_path, "pbcopy", "pbpaste")))
    assert clipboard.tool() == clipboard.ClipboardTool(copy=["pbcopy"], paste=["pbpaste"], clear=["pbcopy"])


def test_tool_pins_wlcopy_argv(tmp_path, monkeypatch):
    """wl-paste appends a newline by default; `-n` suppresses it so the clearer's hash matches."""
    from talkub import clipboard

    monkeypatch.setenv("PATH", str(_bin_dir_with(tmp_path, "wl-copy", "wl-paste")))
    assert clipboard.tool() == clipboard.ClipboardTool(
        copy=["wl-copy"], paste=["wl-paste", "-n"], clear=["wl-copy", "--clear"]
    )


def test_tool_pins_xclip_argv(tmp_path, monkeypatch):
    from talkub import clipboard

    monkeypatch.setenv("PATH", str(_bin_dir_with(tmp_path, "xclip")))
    assert clipboard.tool() == clipboard.ClipboardTool(
        copy=["xclip", "-selection", "clipboard"],
        paste=["xclip", "-selection", "clipboard", "-o"],
        clear=["xclip", "-selection", "clipboard"],
    )


def test_tool_returns_none_when_nothing_is_on_path(monkeypatch, tmp_path):
    _hide_real_tools(monkeypatch, tmp_path)
    from talkub import clipboard

    assert clipboard.tool() is None


def test_copy_sends_the_value_on_stdin_never_in_argv(spy):
    from talkub import clipboard

    pbcopy = spy("pbcopy").on(read_stdin=True)
    found = clipboard.copy(Runner(), SECRET)
    assert found.copy == ["pbcopy"]
    [call] = pbcopy.calls
    assert call["stdin"] == SECRET.decode()
    assert SECRET.decode() not in call["args"]


def test_copy_raises_a_validation_error_when_no_tool_is_found(monkeypatch, tmp_path):
    _hide_real_tools(monkeypatch, tmp_path)
    from talkub import clipboard

    with pytest.raises(ValidationError) as exc:
        clipboard.copy(Runner(), SECRET)
    assert "--print" in exc.value.hint


def test_copy_raises_command_failed_when_the_tool_exits_non_zero(fake_bin):
    from talkub import clipboard

    fake_bin("pbcopy", "echo 'pbcopy: no display' >&2\nexit 7\n")
    with pytest.raises(CommandFailed) as exc:
        clipboard.copy(Runner(), SECRET)
    assert exc.value.hint == "use --print"


def test_copy_does_not_hang_on_a_backgrounded_child_holding_stdout(fake_bin):
    """xclip (always) and wl-copy (usually) fork a background owner of the selection that
    inherits stdout/stderr; capturing with a pipe (as Runner.run does) would hang waiting for
    that background child's stdout to close."""
    from talkub import clipboard

    fake_bin("pbcopy", "sh -c 'sleep 5' & exit 0\n")
    started = time.monotonic()
    clipboard.copy(Runner(), SECRET)
    assert time.monotonic() - started < 2.0


def test_schedule_clear_spawns_the_clearer_with_only_the_digest_on_stdin(monkeypatch):
    from talkub import clipboard

    calls = []

    class RecordingRunner(Runner):
        def spawn_detached(self, argv, *, input):
            calls.append((list(argv), input))

    clipboard.schedule_clear(RecordingRunner(), SECRET)
    [(argv, sent)] = calls
    assert argv == [sys.executable, "-I", "-m", "talkub.clipboard_clear"]  # -I: cwd not on sys.path
    assert sent == hashlib.sha256(SECRET).hexdigest().encode()
    assert SECRET not in sent
    assert SECRET.decode() not in argv


def test_spawn_detached_feeds_stdin_to_a_real_child_and_it_runs_in_its_own_session(tmp_path):
    out = tmp_path / "out"
    code = (
        "import os, sys\n"
        "open(sys.argv[1] + '.sid', 'w').write(str(os.getsid(0)))\n"
        "open(sys.argv[1], 'wb').write(sys.stdin.buffer.read())\n"
    )
    Runner().spawn_detached([sys.executable, "-c", code, str(out)], input=b"hello-detached")

    deadline = time.monotonic() + 5
    while not out.exists() and time.monotonic() < deadline:
        time.sleep(0.05)

    assert out.read_bytes() == b"hello-detached"
    child_sid = int((tmp_path / "out.sid").read_text())
    assert child_sid != os.getsid(0)


def test_clipboard_clear_clears_when_the_clipboard_still_matches(monkeypatch, spy):
    from talkub import clipboard_clear

    digest = hashlib.sha256(SECRET).hexdigest()
    spy("pbpaste").on(stdout=SECRET.decode())
    pbcopy = spy("pbcopy").on(read_stdin=True)
    monkeypatch.setenv("TALKUB_CLIPBOARD_CLEAR_SECONDS", "0")
    monkeypatch.setattr(sys, "stdin", io.StringIO(digest))

    clipboard_clear.main()

    [call] = pbcopy.calls
    assert call["stdin"] == ""
    assert digest not in call["args"]


def test_clipboard_clear_leaves_the_clipboard_alone_when_it_changed(monkeypatch, spy):
    from talkub import clipboard_clear

    spy("pbpaste").on(stdout="something-else-entirely")
    pbcopy = spy("pbcopy").on(read_stdin=True)
    monkeypatch.setenv("TALKUB_CLIPBOARD_CLEAR_SECONDS", "0")
    monkeypatch.setattr(sys, "stdin", io.StringIO(hashlib.sha256(SECRET).hexdigest()))

    clipboard_clear.main()

    assert pbcopy.calls == []


def test_clipboard_clear_clears_wayland_with_the_dash_dash_clear_flag(monkeypatch, spy):
    from talkub import clipboard, clipboard_clear

    wl_tool = clipboard.ClipboardTool(copy=["wl-copy"], paste=["wl-paste", "-n"], clear=["wl-copy", "--clear"])
    monkeypatch.setattr(clipboard, "tool", lambda: wl_tool)
    spy("wl-paste").on(stdout=SECRET.decode())
    wlcopy = spy("wl-copy").on(read_stdin=True)
    monkeypatch.setenv("TALKUB_CLIPBOARD_CLEAR_SECONDS", "0")
    monkeypatch.setattr(sys, "stdin", io.StringIO(hashlib.sha256(SECRET).hexdigest()))

    clipboard_clear.main()

    [call] = wlcopy.calls
    assert call["args"] == ["--clear"]


def test_clipboard_clear_end_to_end_via_schedule_clear(monkeypatch, spy):
    """A real detached child (python -I -m talkub.clipboard_clear) actually clears the clipboard."""
    from talkub import clipboard

    monkeypatch.setenv("TALKUB_CLIPBOARD_CLEAR_SECONDS", "0")
    spy("pbpaste").on(stdout=SECRET.decode())
    pbcopy = spy("pbcopy").on(read_stdin=True)

    clipboard.schedule_clear(Runner(), SECRET)

    deadline = time.monotonic() + 5
    while not pbcopy.calls and time.monotonic() < deadline:
        time.sleep(0.05)

    [call] = pbcopy.calls
    assert call["stdin"] == ""


def test_clipboard_clear_does_nothing_when_no_tool_is_found(monkeypatch, tmp_path):
    from talkub import clipboard_clear

    _hide_real_tools(monkeypatch, tmp_path)
    monkeypatch.setenv("TALKUB_CLIPBOARD_CLEAR_SECONDS", "0")
    monkeypatch.setattr(sys, "stdin", io.StringIO(hashlib.sha256(SECRET).hexdigest()))

    clipboard_clear.main()  # must not raise


@pytest.mark.parametrize(
    ("raw", "expected"), [(None, 45.0), ("0", 0.0), ("2.5", 2.5), ("abc", 45.0), ("-1", 45.0), ("", 45.0)]
)
def test_clear_seconds(monkeypatch, raw, expected):
    from talkub import clipboard

    if raw is None:
        monkeypatch.delenv("TALKUB_CLIPBOARD_CLEAR_SECONDS", raising=False)
    else:
        monkeypatch.setenv("TALKUB_CLIPBOARD_CLEAR_SECONDS", raw)
    assert clipboard.clear_seconds() == expected
