"""Clipboard copy and the detached clearer: the secret value must never reach argv/env/files."""

from __future__ import annotations

import hashlib
import io
import os
import sys
import time

import pytest

from talkub.errors import ValidationError
from talkub.runner import Runner

SECRET = b"top-secret-value"


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


def test_schedule_clear_spawns_the_clearer_with_only_the_digest_on_stdin(monkeypatch):
    from talkub import clipboard

    calls = []

    class RecordingRunner(Runner):
        def spawn_detached(self, argv, *, input):
            calls.append((list(argv), input))

    clipboard.schedule_clear(RecordingRunner(), SECRET)
    [(argv, sent)] = calls
    assert argv == [sys.executable, "-m", "talkub.clipboard_clear"]
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


def test_clipboard_clear_does_nothing_when_no_tool_is_found(monkeypatch, tmp_path):
    from talkub import clipboard_clear

    _hide_real_tools(monkeypatch, tmp_path)
    monkeypatch.setenv("TALKUB_CLIPBOARD_CLEAR_SECONDS", "0")
    monkeypatch.setattr(sys, "stdin", io.StringIO(hashlib.sha256(SECRET).hexdigest()))

    clipboard_clear.main()  # must not raise
