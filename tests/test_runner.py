import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

from talkub.errors import CommandFailed, ToolMissing
from talkub.runner import Runner, child_env, require_tool


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


def test_run_uninterruptible_writes_output_privately_and_returns_the_exit_code(tmp_path):
    out = tmp_path / "out"
    assert Runner().run_uninterruptible([sys.executable, "-c", SLOW_CHILD], output=out) == 3
    assert out.stat().st_mode & 0o777 == 0o600
    assert sorted(out.read_text().split()) == ["ERR", "MARKER"]


def test_run_uninterruptible_refuses_an_existing_output_file(tmp_path):
    out = tmp_path / "out"
    out.write_text("old")
    with pytest.raises(FileExistsError):
        Runner().run_uninterruptible([sys.executable, "-c", "pass"], output=out)


@pytest.mark.parametrize(("signum", "expected"), [(signal.SIGINT, KeyboardInterrupt), (signal.SIGTERM, SystemExit)])
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
    assert sorted(out.read_text().split()) == ["ERR", "MARKER"]  # the child was not killed
    assert f"{Path(sys.executable).name} is still running; waiting for it to finish (do not kill it)" in capfd.readouterr().err
    assert {sig: signal.getsignal(sig) for sig in before} == before


def test_run_uninterruptible_new_session(tmp_path):
    out = tmp_path / "out"
    Runner().run_uninterruptible([sys.executable, "-c", "import os; print(os.getsid(0) == os.getpid())"], output=out)
    assert out.read_text().strip() == "True"
