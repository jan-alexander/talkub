import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from talkub.core.errors import CommandFailed, ToolMissing
from talkub.core.runner import Runner, child_env, pipe_status, require_tool


def test_child_env_strips_key_material(monkeypatch):
    monkeypatch.setenv("SOPS_AGE_KEY", "AGE-SECRET-KEY-1X")
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", "/tmp/k")
    monkeypatch.setenv("TALKUB_IDENTITY", "/tmp/i")
    monkeypatch.setenv("TALKUB_TEST_MARKER", "kept")
    env = child_env()
    assert "SOPS_AGE_KEY" not in env
    assert "SOPS_AGE_KEY_FILE" not in env
    assert "TALKUB_IDENTITY" not in env
    assert env["TALKUB_TEST_MARKER"] == "kept"


def test_child_env_extra_wins_after_filtering(monkeypatch):
    monkeypatch.setenv("SOPS_AGE_KEY", "from-parent")
    assert child_env({"SOPS_AGE_KEY": "explicit"})["SOPS_AGE_KEY"] == "explicit"


def test_require_tool_missing():
    with pytest.raises(ToolMissing) as exc:
        require_tool("definitely-not-a-real-tool-xyz")
    assert "definitely-not-a-real-tool-xyz" in str(exc.value)


def test_run_captures_stdout_and_feeds_stdin():
    code = "import sys; sys.stdout.write(sys.stdin.read().upper())"
    assert Runner().run([sys.executable, "-c", code], input=b"abc").stdout == b"ABC"


def test_run_raises_with_stderr_as_hint():
    code = "import sys; sys.stderr.write('boom'); sys.exit(3)"
    with pytest.raises(CommandFailed) as exc:
        Runner().run([sys.executable, "-c", code])
    assert "exited with 3" in str(exc.value)
    assert exc.value.hint == "boom"


def test_run_without_check_returns_returncode():
    assert Runner().run([sys.executable, "-c", "raise SystemExit(4)"], check=False).returncode == 4


def test_run_timeout_without_check_returns_124():
    proc = Runner().run([sys.executable, "-c", "import time; time.sleep(30)"], check=False, timeout=0.5)
    assert proc.returncode == 124
    assert b"timed out after 0.5s" in proc.stderr


def test_run_timeout_with_check_raises():
    with pytest.raises(CommandFailed) as exc:
        Runner().run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5)
    assert "timed out after 0.5s" in str(exc.value)


def test_run_child_does_not_inherit_key(monkeypatch):
    monkeypatch.setenv("SOPS_AGE_KEY", "secret")
    code = "import os; print(os.environ.get('SOPS_AGE_KEY', 'absent'))"
    assert Runner().run([sys.executable, "-c", code]).stdout.strip() == b"absent"


def test_call_returns_exit_code():
    assert Runner().call([sys.executable, "-c", "raise SystemExit(7)"]) == 7


def test_call_maps_signal_death_to_shell_exit_code():
    code = "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"
    assert Runner().call([sys.executable, "-c", code]) == 143


SLOW_CHILD = "import sys, time; time.sleep(1); print('MARKER'); sys.stderr.write('ERR\\n'); sys.exit(3)"


def _send_later(signum: int, delay: float = 0.2) -> threading.Timer:
    timer = threading.Timer(delay, os.kill, (os.getpid(), signum))
    timer.start()
    return timer


def test_run_uninterruptible_writes_output_privately_and_returns_the_exit_code(
    tmp_path,
):
    out = tmp_path / "out"
    assert Runner().run_uninterruptible([sys.executable, "-c", SLOW_CHILD], output=out) == 3
    assert out.stat().st_mode & 0o777 == 0o600
    assert sorted(out.read_text().split()) == ["ERR", "MARKER"]


def test_run_uninterruptible_refuses_an_existing_output_file(tmp_path):
    out = tmp_path / "out"
    out.write_text("old")
    with pytest.raises(FileExistsError):
        Runner().run_uninterruptible([sys.executable, "-c", "pass"], output=out)


@pytest.mark.parametrize(
    ("signum", "expected"),
    [(signal.SIGINT, KeyboardInterrupt), (signal.SIGTERM, SystemExit)],
)
def test_run_uninterruptible_waits_for_the_child_on_signals(tmp_path, capfd, signum, expected):
    out = tmp_path / "out"
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    start = time.monotonic()
    timer = _send_later(signum)
    try:
        with pytest.raises(expected):
            Runner().run_uninterruptible([sys.executable, "-c", SLOW_CHILD], output=out)
    finally:
        timer.cancel()
    assert time.monotonic() - start >= 1.0  # raised only after the child finished
    assert sorted(out.read_text().split()) == [
        "ERR",
        "MARKER",
    ]  # the child was not killed
    assert (
        f"{Path(sys.executable).name} is still running; waiting for it to finish (do not kill it)"
        in capfd.readouterr().err
    )
    assert {sig: signal.getsignal(sig) for sig in before} == before


class _BrokenStderr:
    """A tty that has hung up: writing to it raises EIO, like a real closed terminal."""

    def write(self, *args, **kwargs):
        raise OSError("I/O error")

    def flush(self):
        raise OSError("I/O error")


def test_run_uninterruptible_survives_a_closed_terminal(tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setattr(sys, "stderr", _BrokenStderr())
    start = time.monotonic()
    timer = _send_later(signal.SIGINT)
    try:
        with pytest.raises(KeyboardInterrupt):
            Runner().run_uninterruptible([sys.executable, "-c", SLOW_CHILD], output=out)
    finally:
        timer.cancel()
    assert time.monotonic() - start >= 1.0  # raised only after the child finished
    assert sorted(out.read_text().split()) == [
        "ERR",
        "MARKER",
    ]  # output file is complete


def test_run_uninterruptible_restores_handlers_when_popen_fails(tmp_path, monkeypatch):
    out = tmp_path / "out"
    signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    before = {sig: signal.getsignal(sig) for sig in signals}
    during: dict[int, object] = {}

    def fake_popen(*args, **kwargs):
        during.update({sig: signal.getsignal(sig) for sig in signals})
        raise OSError("exec failed")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    with pytest.raises(OSError):
        Runner().run_uninterruptible([sys.executable, "-c", "pass"], output=out)
    # the hold handlers were already installed by the time Popen was called
    assert during != before
    # ... and restored once Popen raised
    assert {sig: signal.getsignal(sig) for sig in signals} == before


def test_run_uninterruptible_new_session(tmp_path):
    out = tmp_path / "out"
    Runner().run_uninterruptible(
        [sys.executable, "-c", "import os; print(os.getsid(0) == os.getpid())"],
        output=out,
    )
    assert out.read_text().strip() == "True"


def test_spawn_detached_writes_stdin_to_the_child_and_does_not_wait(tmp_path):
    out = tmp_path / "out"
    code = "import sys; open(sys.argv[1], 'wb').write(sys.stdin.buffer.read())"
    started = time.monotonic()
    Runner().spawn_detached([sys.executable, "-c", code, str(out)], input=b"detached-payload")
    assert time.monotonic() - started < 1.0  # returned without waiting for the child
    deadline = time.monotonic() + 5
    while not out.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert out.read_bytes() == b"detached-payload"


def test_spawn_detached_requires_the_tool():
    with pytest.raises(ToolMissing):
        Runner().spawn_detached(["definitely-not-a-real-tool-xyz"], input=b"")


def test_spawn_detached_does_not_warn_about_the_unwaited_child(recwarn):
    Runner().spawn_detached([sys.executable, "-c", "import time; time.sleep(0.2)"], input=b"")
    assert not any(issubclass(w.category, ResourceWarning) for w in recwarn.list)


def test_spawn_detached_raises_command_failed_on_a_broken_pipe(monkeypatch):
    class FakeStdin:
        def write(self, data):
            raise BrokenPipeError("boom")

        def close(self):
            pass

    class FakeProc:
        def __init__(self):
            self.stdin = FakeStdin()
            self.returncode = None

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: FakeProc())
    with pytest.raises(CommandFailed):
        Runner().spawn_detached([sys.executable, "-c", "pass"], input=b"x")


def test_feed_returns_the_exit_code():
    assert Runner().feed([sys.executable, "-c", "import sys; sys.exit(5)"], input=b"") == 5


def test_feed_does_not_wait_for_a_backgrounded_child_holding_stdout(fake_bin):
    """xclip (always) and wl-copy (usually) fork a background owner of the selection that
    inherits stdout/stderr; capturing with a pipe (as `run` does) would hang on it."""
    fake_bin("slow-bg", "sh -c 'sleep 5' & exit 0\n")
    started = time.monotonic()
    assert Runner().feed(["slow-bg"], input=b"") == 0
    assert time.monotonic() - started < 2.0


def test_feed_passes_stderr_through_but_not_stdout(capfd):
    code = "import sys; sys.stdout.write('OUT'); sys.stderr.write('reason on stderr'); sys.exit(1)"
    assert Runner().feed([sys.executable, "-c", code], input=b"") == 1
    captured = capfd.readouterr()
    assert "reason on stderr" in captured.err
    assert "OUT" not in captured.out


@pytest.mark.parametrize(
    ("codes", "expected"),
    [
        ([0], 0),
        ([3], 3),
        ([0, 0], 0),
        ([1, 0], 1),
        ([0, 1], 1),
        ([2, 3], 3),
        ([2, 0, 0], 2),
        ([141, 0], 0),
        ([141, 1], 1),
        ([0, 141], 141),
        ([141, 141, 0], 0),
    ],
)
def test_pipe_status(codes, expected):
    assert pipe_status(codes) == expected


def test_pipe_status_needs_a_stage():
    with pytest.raises(ValueError):
        pipe_status([])


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


UPPER = "import sys; sys.stdout.write(sys.stdin.read().upper())"


def test_pipeline_passes_output_through_the_stages(capfd):
    stages = [_py("print('abc')"), _py(UPPER), _py("import sys; sys.stdout.write(sys.stdin.read() * 2)")]
    assert Runner().pipeline(stages) == 0
    assert capfd.readouterr().out == "ABC\nABC\n"


def test_pipeline_single_stage_is_like_call(capfd):
    assert Runner().pipeline([_py("print('x'); raise SystemExit(4)")]) == 4
    assert capfd.readouterr().out == "x\n"


def test_pipeline_first_stage_failure_is_not_hidden(capfd):
    assert Runner().pipeline([_py("raise SystemExit(5)"), _py(UPPER)]) == 5


def test_pipeline_rightmost_failure_wins(capfd):
    assert Runner().pipeline([_py("raise SystemExit(2)"), _py("import sys; sys.stdin.read(); sys.exit(3)")]) == 3


def test_pipeline_signal_death_maps_to_shell_code(capfd):
    kill = "import os, signal, sys; sys.stdin.read(); os.kill(os.getpid(), signal.SIGTERM)"
    assert Runner().pipeline([_py("print('x')"), _py(kill)]) == 143


def test_pipeline_env_only_for_the_first_stage(monkeypatch, capfd):
    monkeypatch.setenv("TALKUB_TEST_PARENT", "inherited")
    first = "import os; print(os.environ.get('A'), os.environ.get('B'), os.environ.get('TALKUB_TEST_PARENT'))"
    rest = "import os, sys; print(sys.stdin.read().strip(), os.environ.get('A'), os.environ.get('B'))"
    assert Runner().pipeline([_py(first), _py(rest)], env={"A": "1"}, rest_env={"B": "2"}) == 0
    assert capfd.readouterr().out == "1 None inherited None 2\n"


def test_pipeline_early_close_by_the_reader_is_success():
    # `yes` writes forever and dies of SIGPIPE once `head` exits: without closing the parent's pipe ends
    # it never gets SIGPIPE and this hangs; its 141 must not count as failure. (A Python writer would not
    # do: the interpreter ignores SIGPIPE and exits 1 with BrokenPipeError.) Run in a child process so a
    # hang fails the test via the timeout instead of blocking the suite.
    script = (
        "from talkub.core.runner import Runner; raise SystemExit(Runner().pipeline([['yes'], ['head', '-n', '1']]))"
    )
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=20)
    assert proc.returncode == 0
    assert proc.stdout == b"y\n"


def test_pipeline_missing_tool_starts_nothing(tmp_path):
    marker = tmp_path / "started"
    with pytest.raises(ToolMissing):
        Runner().pipeline([_py(f"open({str(marker)!r}, 'w')"), ["talkub-no-such-tool"]])
    assert not marker.exists()


def test_pipeline_waits_for_all_stages_on_ctrl_c(capfd):
    slow = "import time; time.sleep(1); print('marker')"
    start = time.monotonic()
    timer = _send_later(signal.SIGINT)
    try:
        assert Runner().pipeline([_py(slow), _py(UPPER)]) == 0
    finally:
        timer.cancel()
    assert time.monotonic() - start >= 1.0
    assert capfd.readouterr().out == "MARKER\n"
