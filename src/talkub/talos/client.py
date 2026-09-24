"""talosctl against running nodes, with the talosconfig decrypted for the session only."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from talkub.context import Context
from talkub.ephemeral import decrypted
from talkub.errors import ValidationError
from talkub.runner import Runner, require_tool


@dataclass(frozen=True)
class TalosSession:
    runner: Runner
    talosconfig: Path

    def argv(self, node: str, args: Sequence[str]) -> list[str]:
        return ["talosctl", "--talosconfig", str(self.talosconfig), "-n", node, "-e", node, *args]

    def call(self, node: str, args: Sequence[str]) -> int:
        return self.runner.call(self.argv(node, args))

    def run(self, node: str, args: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        return self.runner.run(self.argv(node, args), check=check)


@contextmanager
def talos_session(ctx: Context) -> Iterator[TalosSession]:
    require_tool("talosctl")
    source = ctx.repo.talosconfig
    if not source.is_file():
        raise ValidationError(f"{ctx.repo.rel(source)} does not exist", hint="it is created by `talkub init`")
    with decrypted(ctx.sops, {"talosconfig": source}) as paths:
        yield TalosSession(ctx.runner, paths["talosconfig"])
