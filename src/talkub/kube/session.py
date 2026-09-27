"""kubectl and helm against the cluster, with the kubeconfig decrypted for the session only."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from talkub.context import Context
from talkub.core.ephemeral import decrypted
from talkub.core.errors import ValidationError
from talkub.core.runner import Runner, require_tool


@dataclass(frozen=True)
class KubeSession:
    runner: Runner
    kubeconfig: Path

    @property
    def env(self) -> dict[str, str]:
        return {"KUBECONFIG": str(self.kubeconfig)}

    def kubectl(
        self, args: Sequence[str], *, input: bytes | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[bytes]:
        return self.runner.run(["kubectl", *args], input=input, env=self.env, check=check)

    def kubectl_call(self, args: Sequence[str]) -> int:
        """Like `kubectl`, but interactive (output straight to the terminal); returns the exit code."""
        return self.runner.call(["kubectl", *args], env=self.env)

    def helm(self, args: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        return self.runner.run(["helm", *args], env=self.env, check=check)


@contextmanager
def kube_session(ctx: Context) -> Iterator[KubeSession]:
    require_tool("kubectl")
    source = ctx.repo.kubeconfig
    if not source.is_file():
        raise ValidationError(
            f"{ctx.repo.rel(source)} does not exist",
            hint="it is created by `talkub bootstrap`",
        )
    with decrypted(ctx.sops, {"kubeconfig": source}) as paths:
        yield KubeSession(ctx.runner, paths["kubeconfig"])
