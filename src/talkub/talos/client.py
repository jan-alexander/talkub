"""talosctl against running nodes, with the talosconfig decrypted for the session only."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from talkub.context import Context
from talkub.ephemeral import decrypted, write_private
from talkub.errors import ValidationError
from talkub.runner import Runner, require_tool
from talkub.talos import inspect


def _print_redacted(text: str, *, file=None) -> None:
    if text:
        print(inspect.redact_diff(text), end="" if text.endswith("\n") else "\n", file=file, flush=True)


@dataclass(frozen=True)
class TalosSession:
    runner: Runner
    talosconfig: Path

    def argv(self, node: str, args: Sequence[str]) -> list[str]:
        return ["talosctl", "--talosconfig", str(self.talosconfig), "-n", node, "-e", node, *args]

    def argv_global(self, args: Sequence[str], endpoints: Sequence[str]) -> list[str]:
        return ["talosctl", "--talosconfig", str(self.talosconfig), "-e", ",".join(endpoints), *args]

    def call(self, node: str, args: Sequence[str]) -> int:
        return self.runner.call(self.argv(node, args))

    def run(
        self, node: str, args: Sequence[str], *, check: bool = True, timeout: float | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        return self.runner.run(self.argv(node, args), check=check, timeout=timeout)

    def run_global(
        self, args: Sequence[str], endpoints: Sequence[str], *, check: bool = True, new_session: bool = False
    ) -> subprocess.CompletedProcess[bytes]:
        """Like `run`, but against `endpoints` only (`-e a,b`, no `-n`) — for calls that are not
        node-scoped."""
        return self.runner.run(self.argv_global(args, endpoints), check=check, new_session=new_session)

    def call_redacted(self, node: str, args: Sequence[str]) -> int:
        """Like `call`, but captures the output and redacts secrets (`inspect.redact_diff`)
        before printing it, instead of letting talosctl write straight to the terminal — some
        talosctl output (config diffs, dry-runs) can contain machine CA keys, auth tokens, …"""
        result = self.run(node, args, check=False)
        _print_redacted(result.stdout.decode(errors="replace"))
        _print_redacted(result.stderr.decode(errors="replace"), file=sys.stderr)
        return result.returncode

    def call_global_redacted(
        self,
        args: Sequence[str],
        endpoints: Sequence[str],
        *,
        raw_to: Path | None = None,
        new_session: bool = False,
    ) -> int:
        """Like `call_redacted`, but via `run_global` (endpoints only, no `-n`).

        `raw_to`: also write the unredacted stdout+stderr to this new owner-only (0600) file,
        before anything is printed."""
        result = self.run_global(args, endpoints, check=False, new_session=new_session)
        if raw_to is not None:
            write_private(raw_to, result.stdout + result.stderr)
        _print_redacted(result.stdout.decode(errors="replace"))
        _print_redacted(result.stderr.decode(errors="replace"), file=sys.stderr)
        return result.returncode


@contextmanager
def talos_session(ctx: Context) -> Iterator[TalosSession]:
    require_tool("talosctl")
    source = ctx.repo.talosconfig
    if not source.is_file():
        raise ValidationError(f"{ctx.repo.rel(source)} does not exist", hint="it is created by `talkub init`")
    with decrypted(ctx.sops, {"talosconfig": source}) as paths:
        yield TalosSession(ctx.runner, paths["talosconfig"])


def talos_session_from(runner: Runner, path: Path) -> TalosSession:
    """A `TalosSession` over an already-decrypted talosconfig at `path` (e.g. one held in a
    private temp dir across several calls), instead of the `talos_session(ctx)` context manager."""
    require_tool("talosctl")
    if not path.is_file():
        raise ValidationError(f"{path} does not exist")
    return TalosSession(runner, path)
