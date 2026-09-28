"""Helpers shared by the test modules."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from talkub.core.runner import Runner


def need(*tools: str) -> pytest.MarkDecorator:
    """Skip a test when one of the external binaries is not installed.

    With TALKUB_TESTS_REQUIRE_TOOLS set (CI), a missing tool fails the collection instead, so a
    green run means the whole suite ran.
    """
    missing = [tool for tool in tools if shutil.which(tool) is None]
    if missing and os.environ.get("TALKUB_TESTS_REQUIRE_TOOLS"):
        raise RuntimeError(f"missing tools: {', '.join(missing)} (TALKUB_TESTS_REQUIRE_TOOLS is set)")
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

    def run(
        self,
        argv,
        *,
        input=None,
        env=None,
        cwd=None,
        check=True,
        passthrough_stderr=False,
        timeout=None,
    ):
        self.calls.append(Call(list(argv), dict(env or {}), input, passthrough_stderr))
        if self.on_run is not None:
            self.on_run(list(argv))
        return subprocess.CompletedProcess(list(argv), 0, self.outputs.get(argv[0], b""), b"")

    def call(self, argv, *, env=None, cwd=None):
        self.calls.append(Call(list(argv), dict(env or {})))
        return self.exit_code


DEFAULT_SCHEMATIC_ID = "376567988ad370138ad8b2698212367b8edcb69b5fd68c80be1f2ec7d603b4ba"


def sample_settings():
    from talkub.config import NodeSpec, SchematicSpec, Settings
    from talkub.config.schematic import customization_hash

    return Settings(
        name="test",
        endpoint="https://10.0.0.10:6443",
        talos_version="v1.14.0",
        kubernetes_version="1.37.0",
        platform="metal",
        schematics={"metal": SchematicSpec(id=DEFAULT_SCHEMATIC_ID, hash=customization_hash([], []))},
        nodes={"cp-01": NodeSpec(ip="10.0.0.10")},
    )


SPY_SCRIPT = r"""
import json
import os
import sys

log_path, rules_path = sys.argv[1], sys.argv[2]
args = sys.argv[3:]
rules = json.load(open(rules_path)) if os.path.exists(rules_path) else []
rule = next((r for r in rules if all(word in args for word in r["match"])), None)
if rule is not None and rule.get("exec"):
    os.execv(rule["exec"], [rule["exec"], *args])
files = {}
for flag in ("--talosconfig", "--file", "-f", "--values", "--cert"):
    if flag in args[:-1]:
        files[flag] = os.path.isfile(args[args.index(flag) + 1])
kubeconfig = os.environ.get("KUBECONFIG", "")
entry = {"args": args, "files": files, "kubeconfig": bool(kubeconfig) and os.path.isfile(kubeconfig)}
if rule is not None and rule.get("sleep"):
    import time
    time.sleep(rule["sleep"])
if rule is not None and rule.get("read_stdin"):
    entry["stdin"] = sys.stdin.read()
with open(log_path, "a") as fh:
    fh.write(json.dumps(entry) + "\n")
if rule is not None:
    sys.stdout.write(rule.get("stdout", ""))
    sys.stderr.write(rule.get("stderr", ""))
    sys.exit(rule.get("rc", 0))
"""


@dataclass
class Spy:
    """A fake binary on PATH (conftest fixture `spy`).

    Every call is logged (unless forwarded to the real binary); the first rule whose
    words all appear in the arguments decides stdout/stderr/exit code (default: empty, 0).
    """

    name: str
    directory: Path
    real_path: str | None
    rules: list[dict] = field(default_factory=list)

    @property
    def log(self) -> Path:
        return self.directory / f"{self.name}.log"

    @property
    def rules_file(self) -> Path:
        return self.directory / f"{self.name}.rules.json"

    def _save(self) -> None:
        self.rules_file.write_text(json.dumps(self.rules))

    def on(
        self,
        *words: str,
        stdout: str = "",
        stderr: str = "",
        rc: int = 0,
        read_stdin: bool = False,
    ) -> Spy:
        """Answer calls whose arguments contain all `words`."""
        self.rules.append(
            {
                "match": list(words),
                "stdout": stdout,
                "stderr": stderr,
                "rc": rc,
                "read_stdin": read_stdin,
            }
        )
        self._save()
        return self

    def exec(self, *words: str, program: str) -> Spy:
        """Run `program` (with the talosctl arguments) for calls whose arguments contain all `words`;
        not logged unless `program` logs itself."""
        self.rules.append({"match": list(words), "exec": program})
        self._save()
        return self

    def real(self, *words: str) -> Spy:
        """Forward calls whose arguments contain all `words` to the real binary (not logged)."""
        if self.real_path is None:
            pytest.skip(f"missing tools: {self.name}")
        self.rules.append({"match": list(words), "exec": self.real_path})
        self._save()
        return self

    @property
    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    @property
    def argvs(self) -> list[list[str]]:
        return [call["args"] for call in self.calls]


def write_chart_app(repo, key: str, repo_url: str, chart: str, version: str, namespace: str) -> None:
    """`gitops/apps/<key>.yaml` (as talkub renders it) plus an empty `gitops/values/<key>.yaml`."""
    from talkub.argocd import apps
    from talkub.config import ChartSpec
    from talkub.core.fsutil import atomic_write

    values_file = f"gitops/values/{key}.yaml"
    atomic_write(repo.root / values_file, b"{}\n")
    text = apps.helm_app(
        key,
        ChartSpec(repo_url, chart, version),
        "https://git.example.com/x.git",
        values_file,
        namespace,
    )
    atomic_write(repo.gitops_dir / "apps" / f"{key}.yaml", text.encode())
