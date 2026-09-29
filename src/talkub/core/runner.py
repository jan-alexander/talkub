"""The only place that starts subprocesses.

Every child gets a filtered environment: key material is removed unless the
caller passes it back in explicitly via `env`.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path

from talkub.core.errors import CommandFailed, ToolMissing

SENSITIVE_ENV = ("SOPS_AGE_KEY", "SOPS_AGE_KEY_FILE", "TALKUB_IDENTITY")

INSTALL_HINTS = {
    "age": "brew install age",
    "age-keygen": "brew install age",
    "age-plugin-se": "brew install age-plugin-se",
    "age-plugin-yubikey": "brew install age-plugin-yubikey",
    "git": "brew install git",
    "helm": "brew install helm",
    "kubeconform": "brew install kubeconform",
    "kubectl": "brew install kubectl",
    "kubeseal": "brew install kubeseal",
    "sops": "brew install sops",
    "ssh-keygen": "part of OpenSSH (preinstalled on macOS)",
    "ssh-keyscan": "part of OpenSSH (preinstalled on macOS)",
    "talosctl": "brew install siderolabs/tap/talosctl",
}


def child_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """The parent environment minus key material, plus `extra`."""
    env = {key: value for key, value in os.environ.items() if key not in SENSITIVE_ENV}
    if extra:
        env.update(extra)
    return env


def require_tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise ToolMissing(f"required tool '{name}' not found in PATH", hint=INSTALL_HINTS.get(name))
    return path


SIGPIPE_EXIT = 128 + signal.SIGPIPE


def pipe_status(codes: Sequence[int]) -> int:
    """Exit code of a pipeline, like `set -o pipefail`: the rightmost non-zero stage code. A stage before
    the last that died of SIGPIPE only saw its reader stop early (`… | head`), which is not a failure."""
    if not codes:
        raise ValueError("a pipeline needs at least one stage")
    *front, last = codes
    relevant = [0 if code == SIGPIPE_EXIT else code for code in front] + [last]
    return next((code for code in reversed(relevant) if code != 0), 0)


class Runner:
    def run(
        self,
        argv: Sequence[str],
        *,
        input: bytes | None = None,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        check: bool = True,
        passthrough_stderr: bool = False,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run non-interactively and capture stdout.

        `passthrough_stderr` lets prompts (e.g. "touch your YubiKey") reach the terminal.
        After `timeout` seconds the child is killed and treated as exit code 124 (like timeout(1)).
        """
        require_tool(argv[0])
        try:
            proc = subprocess.run(
                list(argv),
                input=input,
                stdout=subprocess.PIPE,
                stderr=None if passthrough_stderr else subprocess.PIPE,
                env=child_env(env),
                cwd=cwd,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            message = f"{Path(argv[0]).name} timed out after {timeout:g}s"
            if check:
                raise CommandFailed(message) from None
            return subprocess.CompletedProcess(list(argv), 124, b"", message.encode())
        if check and proc.returncode != 0:
            stderr = (proc.stderr or b"").decode(errors="replace").strip()
            raise CommandFailed(
                f"{Path(argv[0]).name} exited with {proc.returncode}",
                hint=stderr or None,
            )
        return proc

    def run_uninterruptible(self, argv: Sequence[str], *, output: Path, env: Mapping[str, str] | None = None) -> int:
        """Run a step that must not be interrupted halfway and return its exit code.

        stdout+stderr go straight to `output` (created, owner-only 0600; must not exist), so they
        reach disk while the child runs and survive even if talkub itself is killed. The child runs
        in its own session (a terminal Ctrl-C does not reach it); SIGINT/SIGTERM/SIGHUP sent to
        talkub are held until the child has exited — it is never killed — and then re-raised as
        the previous handler would have handled them.
        """
        require_tool(argv[0])
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        on_main_thread = threading.current_thread() is threading.main_thread()
        received: list[int] = []

        def hold(signum, frame):
            if not received:
                try:
                    print(
                        f"{Path(argv[0]).name} is still running; waiting for it to finish (do not kill it)",
                        file=sys.stderr,
                        flush=True,
                    )
                except OSError:
                    pass  # e.g. the terminal hung up (EIO); keep waiting regardless
            received.append(signum)

        previous = {sig: signal.signal(sig, hold) for sig in _HELD_SIGNALS} if on_main_thread else {}
        try:
            try:
                proc = subprocess.Popen(
                    list(argv),
                    stdin=subprocess.DEVNULL,
                    stdout=fd,
                    stderr=subprocess.STDOUT,
                    env=child_env(env),
                    start_new_session=True,
                )
            finally:
                os.close(fd)
            if not on_main_thread:
                return proc.wait()
            code = proc.wait()  # the handler does not raise, so wait() resumes after each signal
        finally:
            for sig, old in previous.items():
                signal.signal(sig, old)
        if received:
            _redeliver(received[0], previous[received[0]])
        return code

    def spawn_detached(self, argv: Sequence[str], *, input: bytes) -> None:
        """Start `argv` in its own session, feed it `input` on stdin, and do not wait for it.

        stdout/stderr are discarded. Used for the clipboard clearer: the caller returns to the
        user immediately while the child sleeps and later clears the clipboard.
        """
        require_tool(argv[0])
        proc = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=child_env(None),
            start_new_session=True,
            close_fds=True,
        )
        assert proc.stdin is not None
        try:
            proc.stdin.write(input)
            proc.stdin.close()
        except BrokenPipeError as exc:
            raise CommandFailed(f"{Path(argv[0]).name} closed its stdin before receiving input") from exc
        # We deliberately never wait() on a detached child; mark it as already reaped so
        # Popen.__del__ does not emit a ResourceWarning about it still running.
        proc.returncode = 0

    def feed(self, argv: Sequence[str], *, input: bytes, env: Mapping[str, str] | None = None) -> int:
        """Run `argv`, feeding it `input` on stdin, and return its exit code.

        Unlike `run`, nothing is captured with a pipe: stdout is discarded and stderr goes to the
        terminal (so a failure reason is visible). Some tools (clipboard copy tools: xclip always,
        wl-copy usually) fork a background process that holds the selection and inherits
        stdout/stderr; capturing with a pipe would then hang waiting for that child to close it.
        """
        require_tool(argv[0])
        proc = subprocess.run(
            list(argv),
            input=input,
            stdout=subprocess.DEVNULL,
            env=child_env(env),
            check=False,
        )
        return proc.returncode

    def call(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> int:
        """Run interactively with inherited stdio and return the exit code.

        A child killed by signal N yields 128 + N, like a shell does.
        """
        require_tool(argv[0])
        proc = subprocess.Popen(list(argv), env=child_env(env), cwd=cwd)
        while True:
            try:
                code = proc.wait()
                return 128 - code if code < 0 else code
            except KeyboardInterrupt:
                # The child got SIGINT too (same process group); let it decide when to exit.
                continue
            except BaseException:
                proc.terminate()
                proc.wait()
                raise

    def pipeline(
        self,
        stages: Sequence[Sequence[str]],
        *,
        env: Mapping[str, str] | None = None,
        rest_env: Mapping[str, str] | None = None,
    ) -> int:
        """Run `stages` connected stdout → stdin like a shell pipe, with inherited stdin (first stage),
        stdout (last stage) and stderr (all). Only the first stage gets `env`; the others get `rest_env`.
        Returns pipe_status() of the stage codes; a stage killed by signal N counts as 128 + N."""
        for stage in stages:
            require_tool(stage[0])
        procs: list[subprocess.Popen[bytes]] = []
        previous = None
        try:
            for index, stage in enumerate(stages):
                proc = subprocess.Popen(
                    list(stage),
                    stdin=previous,
                    stdout=None if index == len(stages) - 1 else subprocess.PIPE,
                    env=child_env(env if index == 0 else rest_env),
                )
                if previous is not None:
                    previous.close()  # the child holds its own copy; ours would keep the writer from SIGPIPE
                previous = proc.stdout
                procs.append(proc)
        except BaseException:
            if previous is not None:
                previous.close()
            _stop(procs)
            raise
        codes: list[int] = []
        for proc in procs:
            while True:
                try:
                    code = proc.wait()
                    break
                except KeyboardInterrupt:
                    # The stages got SIGINT too (same process group); let them decide when to exit.
                    continue
                except BaseException:
                    _stop(procs)
                    raise
            codes.append(128 - code if code < 0 else code)
        return pipe_status(codes)


def _stop(procs: Sequence[subprocess.Popen[bytes]]) -> None:
    for proc in procs:
        if proc.poll() is None:
            proc.terminate()
    for proc in procs:
        proc.wait()


_HELD_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def _redeliver(signum: int, handler) -> None:
    """Handle a held signal the way `handler` (the one active before it was held) would have."""
    if callable(handler):
        handler(signum, None)
    elif handler == signal.SIG_IGN:
        return
    elif signum == signal.SIGINT:
        raise KeyboardInterrupt
    else:
        raise SystemExit(128 + signum)
