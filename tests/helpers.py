"""Helpers shared by the test modules."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from talkub.runner import Runner


def need(*tools: str) -> pytest.MarkDecorator:
    """Skip a test when one of the external binaries is not installed."""
    missing = [tool for tool in tools if shutil.which(tool) is None]
    return pytest.mark.skipif(bool(missing), reason=f"missing tools: {', '.join(missing)}")


@dataclass
class Call:
    argv: list[str]
    env: dict[str, str]
    input: bytes | None = None
    passthrough_stderr: bool = False


@dataclass
class FakeRunner(Runner):
    """Records calls instead of running them. `outputs` maps argv[0] to stdout."""

    outputs: dict[str, bytes] = field(default_factory=dict)
    exit_code: int = 0
    on_run: Callable[[list[str]], None] | None = None
    calls: list[Call] = field(default_factory=list)

    def run(self, argv, *, input=None, env=None, cwd=None, check=True, passthrough_stderr=False):
        self.calls.append(Call(list(argv), dict(env or {}), input, passthrough_stderr))
        if self.on_run is not None:
            self.on_run(list(argv))
        return subprocess.CompletedProcess(list(argv), 0, self.outputs.get(argv[0], b""), b"")

    def call(self, argv, *, env=None, cwd=None):
        self.calls.append(Call(list(argv), dict(env or {})))
        return self.exit_code


def sample_settings():
    from talkub.repo import NodeSpec, Settings

    return Settings(
        name="test",
        endpoint="https://10.0.0.10:6443",
        talos_version="v1.14.0",
        kubernetes_version="1.37.0",
        install_image="factory.talos.dev/metal-installer/abc",
        nodes={"cp-01": NodeSpec(ip="10.0.0.10")},
    )
