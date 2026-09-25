"""The only place that starts subprocesses.

Every child gets a filtered environment: key material is removed unless the
caller passes it back in explicitly via `env`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from talkub.errors import CommandFailed, ToolMissing

SENSITIVE_ENV = ("SOPS_AGE_KEY", "SOPS_AGE_KEY_FILE", "TALKUB_IDENTITY")

INSTALL_HINTS = {
    "age": "brew install age",
    "age-keygen": "brew install age",
    "age-plugin-se": "brew install age-plugin-se",
    "age-plugin-yubikey": "brew install age-plugin-yubikey",
    "git": "brew install git",
    "helm": "brew install helm",
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
        new_session: bool = False,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run non-interactively and capture stdout.

        `passthrough_stderr` lets prompts (e.g. "touch your YubiKey") reach the terminal.
        After `timeout` seconds the child is killed and treated as exit code 124 (like timeout(1)).
        `new_session` starts the child in its own session, so a terminal Ctrl-C does not reach it
        (for steps that must not be interrupted halfway).
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
                start_new_session=new_session,
            )
        except subprocess.TimeoutExpired:
            message = f"{Path(argv[0]).name} timed out after {timeout:g}s"
            if check:
                raise CommandFailed(message) from None
            return subprocess.CompletedProcess(list(argv), 124, b"", message.encode())
        if check and proc.returncode != 0:
            stderr = (proc.stderr or b"").decode(errors="replace").strip()
            raise CommandFailed(
                f"{Path(argv[0]).name} exited with {proc.returncode}", hint=stderr or None
            )
        return proc

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
