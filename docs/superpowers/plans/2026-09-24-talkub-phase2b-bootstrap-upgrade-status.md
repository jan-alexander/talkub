# talkub Phase 2b (Bootstrap, Upgrade, Status, Sealed Secrets) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn a described cluster into a running one and keep it current: `bootstrap`, `upgrade talos|k8s`, `status`, `secret seal`, `secret backup-sealing-key`, plus a rolling (health-waiting) `apply`.

**Architecture:** A `KubeSession` (kubeconfig decrypted for the session, like `TalosSession`) runs `kubectl`/`helm`. `talos/inspect.py` parses tool output defensively (JSON where possible; unknown formats give `None`/"unknown", never a crash). `bootstrap.py`, `upgrade.py`, `status.py` and `sealing.py` orchestrate talosctl/kubectl/helm/kubeseal through the `Runner`. Tests use a generic logging "spy" fake binary with per-test rules.

**Tech Stack:** Python ≥ 3.12, Typer, ruamel.yaml, pytest; binaries talosctl 1.14, kubectl, helm 4, kubeseal 0.40, openssl (tests), age, sops.

**Spec:** `docs/superpowers/specs/2026-09-23-talkub-phase2-design.md` (§7 rows bootstrap/upgrade/status/secret, §8 Bootstrap). Phase-1 spec still binds.

## Global Constraints

- All code, identifiers, comments, docstrings and CLI output in **English**.
- `runner.py` is the only module that starts subprocesses; argv lists only.
- Plaintext secrets (kubeconfig, talosconfig, Talos secrets, sealing key, Secret manifests) exist only in memory or inside `ephemeral.decrypted()` / `private_file()` / `private_tempdir()`; they are never passed on a command line (values in argv are visible in `ps`).
- `kubectl`/`helm` get the kubeconfig only via `KUBECONFIG` in the child env from `KubeSession.env`.
- Sealed Secrets controller: name `sealed-secrets-controller`, namespace `kube-system`; key label `sealedsecrets.bitnami.com/sealed-secrets-key`.
- Helm releases: `sealed-secrets` in `kube-system` (values `gitops/values/sealed-secrets.yaml`), `argocd` in `argocd` with `--create-namespace` (values `gitops/values/argocd.yaml`); chart repo/name/version from `.talkub.yaml` `charts`; `--wait --timeout 10m`.
- Health waits: `talosctl -n <mgmt> -e <mgmt> health --wait-timeout 10m` (15m in bootstrap). A cluster counts as bootstrapped iff `kubernetes/kubeconfig.sops.yaml` exists.
- Parsers in `talos/inspect.py` never raise on unexpected input; they return `None` / `"unknown"`.
- Output formats of talosctl (`version`, `get extensions -o json`, `apply-config --dry-run`, `bootstrap` "already" errors) are taken from talosctl docs and must be re-checked on a real node; keep the parsing in `inspect.py` only.
- Commits end with a Co-Authored-By trailer (the model that wrote the commit).
- Run tests with `uv run pytest`; tests needing a binary skip when it is missing.

## Review Focus

1. **Re-running `bootstrap` after a partial failure** must continue without error (etcd already bootstrapped, releases already installed) and must not rewrite an unchanged kubeconfig (test in Task 5).
2. **Sealing key restore order:** with `bootstrap/sealed-secrets-key.sops.yaml` present, the key is applied before the sealed-secrets Helm release, and no new backup is taken (test in Task 5).
3. **Cluster unreachable while sealing:** `secret seal` falls back to `bootstrap/sealed-secrets.pem` with a notice, and fails clearly when neither is available (test in Task 4).
4. **Unreachable node during `upgrade talos`:** the run stops at that node, names it, and upgrades nothing further (test in Task 6).
5. **Unparseable tool output** (unknown talosctl version format, garbage JSON) shows "unknown" in `status` instead of crashing (tests in Tasks 2 and 7).

---

## File Structure

```
src/talkub/
├── kube.py              # NEW: KubeSession, kube_session
├── talos/inspect.py     # NEW: server_version, json_documents, extensions, schematic_id, config_drift,
│                        #      kubelet_versions, application_summary
├── lifecycle.py         # MODIFY: public prepare/ensure_images, is_bootstrapped, wait_healthy, rolling apply
├── sealing.py           # NEW: secret_manifest, check_secret_manifest, sealing_cert, seal, write_sealed,
│                        #      backup_sealing_key, cert_path, key_backup_path
├── bootstrap.py         # NEW: run
├── upgrade.py           # NEW: talos, k8s
├── status.py            # NEW: NodeStatus, StatusReport, collect, render
├── access.py            # MODIFY: KUBECONFIG_HINT
└── cli/{secret,bootstrap,upgrade,status}.py (NEW), cli/setup.py, cli/main.py (MODIFY)
tests/
├── helpers.py           # MODIFY: SPY_SCRIPT, Spy
├── conftest.py          # MODIFY: spy fixture, sealing_cert fixture
└── test_kube.py, test_inspect.py, test_lifecycle.py (extend), test_sealing.py, test_cli_secret.py,
    test_bootstrap.py, test_upgrade.py, test_status.py, test_cli_nodes.py (extend)
```

---

### Task 1: Spy test helper, kube session, init node-name check

**Files:**
- Create: `src/talkub/kube.py`, `tests/test_kube.py`
- Modify: `tests/helpers.py`, `tests/conftest.py`, `src/talkub/cli/setup.py`, `tests/test_cli_nodes.py`

**Interfaces:**
- Consumes: `Context`, `ephemeral.decrypted`, `runner.Runner/require_tool`, `ValidationError`, `nodes.validate_node_name`.
- Produces:
  - `helpers.SPY_SCRIPT: str`, `helpers.Spy(name, directory, real_path)` with `on(*words, stdout="", stderr="", rc=0, read_stdin=False) -> Spy`, `real(*words) -> Spy`, `calls -> list[dict]` (keys `args`, `files`, `kubeconfig`, optional `stdin`), `argvs -> list[list[str]]`
  - conftest fixture `spy` → `make(name: str) -> Spy`
  - `kube.KubeSession(runner, kubeconfig: Path)` with `env -> dict[str, str]`, `kubectl(args, *, input=None, check=True)`, `helm(args, *, check=True)`
  - `kube.kube_session(ctx) -> ContextManager[KubeSession]`

- [ ] **Step 1: Add the spy helper to `tests/helpers.py`**

Add imports `import json`, `from pathlib import Path` at the top (keep existing imports), then append:

```python
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
```

- [ ] **Step 2: Add the `spy` fixture to `tests/conftest.py`**

```python
@pytest.fixture
def spy(fake_bin, tmp_path):
    """spy("kubectl") puts a logging fake `kubectl` in front of PATH and returns its Spy."""
    import sys

    from helpers import SPY_SCRIPT, Spy

    directory = tmp_path / "spies"
    directory.mkdir()
    script = directory / "spy.py"
    script.write_text(SPY_SCRIPT)

    def make(name: str) -> Spy:
        instance = Spy(name=name, directory=directory, real_path=shutil.which(name))
        fake_bin(
            name,
            f'exec "{sys.executable}" "{script}" "{instance.log}" "{instance.rules_file}" "$@"\n',
        )
        return instance

    return make
```

- [ ] **Step 3: Write the failing tests**

`tests/test_kube.py`:

```python
import pytest

from helpers import need
from talkub.context import Context
from talkub.errors import ValidationError
from talkub.kube import kube_session
from talkub.runner import Runner


def test_spy_rules_first_match_and_logging(spy):
    fake = (
        spy("kubectl")
        .on("get", "nodes", stdout="nodes!", rc=3)
        .on("get", stdout="other")
    )
    proc = Runner().run(["kubectl", "get", "nodes"], check=False)
    assert (proc.returncode, proc.stdout) == (3, b"nodes!")
    assert Runner().run(["kubectl", "get", "pods"]).stdout == b"other"
    assert fake.argvs == [["get", "nodes"], ["get", "pods"]]


@need("age", "age-keygen", "sops")
def test_kube_session_exposes_kubeconfig_only_during_session(repo_with_configs, spy):
    kubectl = spy("kubectl").on("get", "nodes", stdout='{"items": []}')
    helm = spy("helm")
    with kube_session(Context.open(repo_with_configs)) as kube:
        assert kube.kubectl(["get", "nodes", "-o", "json"]).stdout == b'{"items": []}'
        kube.helm(["list"])
        path = kube.kubeconfig
        assert path.read_text() == "kube: marker\n"
        assert kube.env == {"KUBECONFIG": str(path)}
    assert not path.exists()
    assert kubectl.calls[0]["kubeconfig"] is True
    assert helm.calls[0]["kubeconfig"] is True


@need("age", "age-keygen", "sops")
def test_kube_session_without_kubeconfig(keyed_repo, spy):
    spy("kubectl")
    with pytest.raises(ValidationError) as exc:
        with kube_session(Context.open(keyed_repo)):
            pass
    assert "bootstrap" in exc.value.hint
```

Append to `tests/test_cli_nodes.py`:

```python
def test_init_rejects_invalid_node_name(tmp_path, capfd, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code = _run(
        [
            "init",
            "demo",
            "--endpoint",
            "https://10.0.0.10:6443",
            "--git-url",
            "https://g",
            "--node",
            "a{x}=10.0.0.10",
            "--static",
            "10.0.0.10/24",
            "--gateway",
            "10.0.0.1",
        ]
    )
    assert code == 1
    assert "invalid node name" in capfd.readouterr().err
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `uv run pytest tests/test_kube.py tests/test_cli_nodes.py -q`
Expected: `ModuleNotFoundError: No module named 'talkub.kube'`; the init test fails with a traceback (KeyError from `str.format`) or exit code other than 1.

- [ ] **Step 5: Implement**

`src/talkub/kube.py`:

```python
"""kubectl and helm against the cluster, with the kubeconfig decrypted for the session only."""

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
class KubeSession:
    runner: Runner
    kubeconfig: Path

    @property
    def env(self) -> dict[str, str]:
        return {"KUBECONFIG": str(self.kubeconfig)}

    def kubectl(
        self, args: Sequence[str], *, input: bytes | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[bytes]:
        return self.runner.run(
            ["kubectl", *args], input=input, env=self.env, check=check
        )

    def helm(
        self, args: Sequence[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[bytes]:
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
```

In `src/talkub/cli/setup.py`, import `validate_node_name` from `talkub.nodes` (extend the existing `from talkub.nodes import …` line) and make `_parse_node` validate the name before anything else uses it:

```python
def _parse_node(value: str) -> tuple[str, NodeSpec]:
    name, sep, ip = value.partition("=")
    if not sep or not name or not ip:
        raise ValidationError(
            f"invalid --node {value!r}", hint="use name=ip, e.g. cp-01=10.0.0.10"
        )
    validate_node_name(name)
    validate_address(ip, "--node")
    return name, NodeSpec(ip=ip)
```

- [ ] **Step 6: Run tests to verify they pass, then the full suite**

Run: `uv run pytest tests/test_kube.py tests/test_cli_nodes.py -q`, then `uv run pytest -q`
Expected: all passed.

- [ ] **Step 7: Commit**

```bash
git add src/talkub/kube.py src/talkub/cli/setup.py tests/helpers.py tests/conftest.py tests/test_kube.py tests/test_cli_nodes.py
git commit -m "feat: kube session and spy test helper; validate init node names"
```

---

### Task 2: Output parsers (`talos/inspect.py`)

**Files:**
- Create: `src/talkub/talos/inspect.py`, `tests/test_inspect.py`

**Interfaces:**
- Produces: `server_version(text) -> str | None`, `json_documents(text) -> list[object]`, `extensions(text) -> dict[str, str]`, `schematic_id(text) -> str | None`, `config_drift(text) -> str` ("in sync" | "changes" | "unknown"), `kubelet_versions(text) -> dict[str, str]`, `application_summary(text) -> dict[str, int] | None`.

- [ ] **Step 1: Write the failing tests**

`tests/test_inspect.py`:

```python
from talkub.talos import inspect

VERSION = """Client:
\tTag:         v1.14.0
\tSHA:         9abd05af
Server:
\tNODE:        10.0.0.10
\tTag:         v1.13.2
\tSHA:         0123abcd
"""

EXTENSIONS = """{
    "node": "10.0.0.10",
    "metadata": {"namespace": "runtime", "type": "ExtensionStatuses.runtime.talos.dev", "id": "0"},
    "spec": {"image": "0.sqsh", "metadata": {"name": "siderolabs/intel-ucode", "version": "20260812"}}
}
{
    "node": "10.0.0.10",
    "metadata": {"namespace": "runtime", "type": "ExtensionStatuses.runtime.talos.dev", "id": "1"},
    "spec": {"image": "1.sqsh", "metadata": {"name": "schematic", "version": "188cef51"}}
}
"""

NODES = """{"items": [
  {"metadata": {"name": "cp-01"}, "status": {"nodeInfo": {"kubeletVersion": "v1.37.0"}}},
  {"metadata": {"name": "w-01"}, "status": {"nodeInfo": {"kubeletVersion": "v1.36.2"}}}
]}"""

APPS = """{"items": [
  {"metadata": {"name": "root"}, "status": {"sync": {"status": "Synced"}, "health": {"status": "Healthy"}}},
  {"metadata": {"name": "argocd"}, "status": {"sync": {"status": "Synced"}, "health": {"status": "Healthy"}}},
  {"metadata": {"name": "app"}, "status": {"sync": {"status": "OutOfSync"}}}
]}"""


def test_server_version():
    assert inspect.server_version(VERSION) == "v1.13.2"
    assert inspect.server_version("Client:\n\tTag: v1.14.0\n") is None
    assert inspect.server_version("garbage") is None


def test_json_documents_stream_and_garbage():
    assert inspect.json_documents('{"a": 1}\n{"b": 2}') == [{"a": 1}, {"b": 2}]
    assert inspect.json_documents('{"a": 1} not json') == [{"a": 1}]
    assert inspect.json_documents("") == []


def test_extensions_and_schematic():
    assert inspect.extensions(EXTENSIONS) == {
        "siderolabs/intel-ucode": "20260812",
        "schematic": "188cef51",
    }
    assert inspect.schematic_id(EXTENSIONS) == "188cef51"
    assert inspect.schematic_id("[]") is None
    assert inspect.schematic_id("{not json") is None


def test_config_drift():
    assert inspect.config_drift("Dry run summary:\nNo changes.\n") == "in sync"
    assert (
        inspect.config_drift("Dry run summary:\nConfig diff:\n\n--- a\n+++ b\n+x\n")
        == "changes"
    )
    assert inspect.config_drift("something else") == "unknown"


def test_kubelet_versions():
    assert inspect.kubelet_versions(NODES) == {"cp-01": "1.37.0", "w-01": "1.36.2"}
    assert inspect.kubelet_versions("oops") == {}


def test_application_summary():
    assert inspect.application_summary(APPS) == {
        "Synced/Healthy": 2,
        "OutOfSync/Unknown": 1,
    }
    assert inspect.application_summary('{"items": []}') == {}
    assert (
        inspect.application_summary("error: the server doesn't have a resource type")
        is None
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_inspect.py -q`
Expected: `ImportError: cannot import name 'inspect'`.

- [ ] **Step 3: Implement `src/talkub/talos/inspect.py`**

```python
"""Parsing talosctl / kubectl output. Unexpected input gives None / "unknown", never an exception.

The talosctl text formats (version, apply-config --dry-run) follow the talosctl docs; keep
all format knowledge here so it can be adjusted in one place.
"""

from __future__ import annotations

import json
import re
from collections import Counter

_SERVER_TAG = re.compile(r"^Server:\s*$.*?^\s*Tag:\s*(\S+)", re.MULTILINE | re.DOTALL)


def server_version(text: str) -> str | None:
    """Talos version of the node from `talosctl version` output."""
    match = _SERVER_TAG.search(text)
    return match.group(1) if match else None


def json_documents(text: str) -> list[object]:
    """Parse a stream of concatenated JSON documents; stops at the first invalid one."""
    decoder = json.JSONDecoder()
    docs: list[object] = []
    index = 0
    while True:
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            return docs
        try:
            doc, index = decoder.raw_decode(text, index)
        except ValueError:
            return docs
        docs.append(doc)


def extensions(text: str) -> dict[str, str]:
    """name -> version from `talosctl get extensions -o json`."""
    result: dict[str, str] = {}
    for doc in json_documents(text):
        if not isinstance(doc, dict):
            continue
        spec = doc.get("spec")
        metadata = spec.get("metadata") if isinstance(spec, dict) else None
        if isinstance(metadata, dict) and isinstance(metadata.get("name"), str):
            result[metadata["name"]] = str(metadata.get("version", ""))
    return result


def schematic_id(text: str) -> str | None:
    """Image Factory images report their schematic ID as the `schematic` extension version."""
    return extensions(text).get("schematic") or None


def config_drift(text: str) -> str:
    """Classify `talosctl apply-config --dry-run` output."""
    lowered = text.lower()
    if "no changes" in lowered:
        return "in sync"
    if "config diff" in lowered:
        return "changes"
    return "unknown"


def _items(text: str) -> list[dict]:
    docs = json_documents(text)
    if (
        len(docs) != 1
        or not isinstance(docs[0], dict)
        or not isinstance(docs[0].get("items"), list)
    ):
        return []
    return [item for item in docs[0]["items"] if isinstance(item, dict)]


def kubelet_versions(text: str) -> dict[str, str]:
    """node name -> kubelet version (without leading v) from `kubectl get nodes -o json`."""
    result: dict[str, str] = {}
    for item in _items(text):
        name = (item.get("metadata") or {}).get("name")
        version = ((item.get("status") or {}).get("nodeInfo") or {}).get(
            "kubeletVersion"
        )
        if isinstance(name, str) and isinstance(version, str):
            result[name] = version.lstrip("v")
    return result


def application_summary(text: str) -> dict[str, int] | None:
    """'<sync>/<health>' -> count from `kubectl get applications -o json`; None if not parseable."""
    docs = json_documents(text)
    if (
        len(docs) != 1
        or not isinstance(docs[0], dict)
        or not isinstance(docs[0].get("items"), list)
    ):
        return None
    counts: Counter[str] = Counter()
    for item in _items(text):
        status = item.get("status") or {}
        sync = (status.get("sync") or {}).get("status") or "Unknown"
        health = (status.get("health") or {}).get("status") or "Unknown"
        counts[f"{sync}/{health}"] += 1
    return dict(counts)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_inspect.py -q`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add src/talkub/talos/inspect.py tests/test_inspect.py
git commit -m "feat: defensive parsers for talosctl and kubectl output"
```

---

### Task 3: Rolling `apply` (health wait) and public prepare helpers

**Files:**
- Modify: `src/talkub/lifecycle.py`, `README.md`, `tests/test_lifecycle.py`

**Interfaces:**
- Consumes: `TalosSession.call`, `Repo.kubeconfig`, spy fixture.
- Produces:
  - `lifecycle.HEALTH_TIMEOUT = "10m"`
  - `lifecycle.ensure_images(ctx, settings, order) -> None` (missing-schematic check + refresh + write)
  - `lifecycle.prepare(ctx, names) -> tuple[Settings, list[str], dict[str, bytes]]` (was `_prepare`)
  - `lifecycle.is_bootstrapped(ctx) -> bool`
  - `lifecycle.wait_healthy(talos, address) -> None` (raises `CommandFailed`)
  - `apply` waits for health after each applied node except the last when `mode in ("auto", "try")` and the cluster is bootstrapped

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_lifecycle.py` (it already imports `pytest`, `lifecycle`, `Context`, `CommandFailed`, `atomic_write`, `node_patch_path`, `render_node_patch`, `NodeSpec` and defines `_calls` and `_add_node`):

```python
def test_apply_waits_for_health_between_nodes(talos_repo, talosctl_spy):
    _add_node(talos_repo, "w-01", "10.0.0.11")
    lifecycle.apply(Context.open(talos_repo), [], confirm=lambda q: True)
    calls = _calls(talosctl_spy)
    health = [i for i, c in enumerate(calls) if " health --wait-timeout 10m" in c]
    assert len(health) == 1
    assert "-n 10.0.0.10" in calls[health[0]]
    assert "10.0.0.11" in calls[health[0] + 1]  # the worker comes after the wait


def test_no_health_wait_without_reboot_mode_or_bootstrap(talos_repo, talosctl_spy):
    _add_node(talos_repo, "w-01", "10.0.0.11")
    lifecycle.apply(
        Context.open(talos_repo), [], mode="no-reboot", confirm=lambda q: True
    )
    assert not any(" health " in c for c in _calls(talosctl_spy))
    talos_repo.kubeconfig.unlink()
    lifecycle.apply(Context.open(talos_repo), [], confirm=lambda q: True)
    assert not any(" health " in c for c in _calls(talosctl_spy))


def test_health_failure_stops_and_reports_progress(talos_repo, spy):
    _add_node(talos_repo, "w-01", "10.0.0.11")
    talosctl = spy("talosctl").real("gen").real("validate").on("health", rc=1)
    with pytest.raises(CommandFailed) as exc:
        lifecycle.apply(Context.open(talos_repo), [], confirm=lambda q: True)
    assert "10.0.0.10" in str(exc.value)
    assert "already applied: cp-01" in exc.value.hint
    assert "not touched: w-01" in exc.value.hint
    assert not any("10.0.0.11" in argv for argv in talosctl.argvs)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_lifecycle.py -q`
Expected: the three new tests fail (no `health` call is made).

- [ ] **Step 3: Refactor and implement in `src/talkub/lifecycle.py`**

Replace the function `_prepare` with these two public functions and add the health helpers (keep everything else):

```python
HEALTH_TIMEOUT = "10m"


def ensure_images(ctx: Context, settings: Settings, order: Sequence[str]) -> None:
    """Every node's platform has a schematic; outdated IDs are resolved and saved."""
    for name in order:
        platform = settings.node_platform(name)
        if platform not in settings.schematics:
            raise ValidationError(
                f"no schematic for platform '{platform}' (used by node '{name}')",
                hint=f"define it with `talkub image add --platform {platform} …` "
                f"or `talkub image show --platform {platform}`",
            )
    platforms = sorted({settings.node_platform(name) for name in order})
    if images.refresh(settings, FactoryClient(), platforms):
        ctx.repo.write_settings(settings)
        print("talkub: updated schematic IDs in .talkub.yaml", file=sys.stderr)


def prepare(
    ctx: Context, names: Sequence[str]
) -> tuple[Settings, list[str], dict[str, bytes]]:
    settings = ctx.repo.load_settings()
    order = ordered_nodes(settings, names)
    ensure_images(ctx, settings, order)
    return settings, order, render_configs(ctx, settings, order)


def is_bootstrapped(ctx: Context) -> bool:
    return ctx.repo.kubeconfig.is_file()


def wait_healthy(talos: TalosSession, address: str) -> None:
    print(f"   waiting for {address} to become healthy", flush=True)
    code = talos.call(address, ["health", "--wait-timeout", HEALTH_TIMEOUT])
    if code != 0:
        raise CommandFailed(
            f"{address} did not become healthy",
            hint=f"talosctl health exited with {code}",
        )
```

Replace every `_prepare(` call with `prepare(` (in `plan`, `apply`, `apply_insecure`). In `apply`, replace the loop with:

```python
rolling = mode in ("auto", "try") and is_bootstrapped(ctx)
applied: list[str] = []
with talos_session(ctx) as talos:
    for index, name in enumerate(order):
        try:
            _apply_one(talos, settings, name, configs[name], mode, confirm, applied)
            if rolling and applied and applied[-1] == name and index < len(order) - 1:
                wait_healthy(talos, settings.mgmt_address(name))
        except CommandFailed as err:
            progress = (
                f"already applied: {', '.join(applied) or 'none'}; "
                f"not touched: {', '.join(order[index + 1 :]) or 'none'}"
            )
            raise CommandFailed(str(err), hint=f"{err.hint}; {progress}") from None
return applied
```

- [ ] **Step 4: Update the README warning**

In `README.md`, replace the paragraph starting with `**Warning:** \`apply\` does not wait` (4 lines) with:

```markdown
Once the cluster is bootstrapped, `apply --mode auto|try` waits for `talosctl health` after each
node before moving on (rolling). Before `talkub bootstrap`, apply nodes one at a time. If `apply`
fails, the error lists the nodes already applied in this run.
```

- [ ] **Step 5: Run tests to verify they pass, then the full suite**

Run: `uv run pytest tests/test_lifecycle.py -q`, then `uv run pytest -q`
Expected: all passed. If an existing test asserted an exact list of talosctl calls for a multi-node apply, extend it with the new `health` call and say so in the report.

- [ ] **Step 6: Commit**

```bash
git add src/talkub/lifecycle.py README.md tests/test_lifecycle.py
git commit -m "feat: rolling apply waits for node health once the cluster is bootstrapped"
```

---

### Task 4: Sealed Secrets (`secret seal`, `secret backup-sealing-key`)

**Files:**
- Create: `src/talkub/sealing.py`, `src/talkub/cli/secret.py`, `tests/test_sealing.py`, `tests/test_cli_secret.py`
- Modify: `src/talkub/cli/main.py`, `tests/conftest.py`

**Interfaces:**
- Consumes: `kube.kube_session`, `KubeSession.env/kubectl`, `ephemeral.private_file`, `atomic_write`, `yamlio`, `Context.sops/keyring`, `require_tool`.
- Produces:
  - `sealing.CONTROLLER: list[str]`, `sealing.KEY_LABEL`, `sealing.cert_path(repo) -> Path`, `sealing.key_backup_path(repo) -> Path`
  - `sealing.secret_manifest(name, namespace, literals, files) -> bytes`
  - `sealing.check_secret_manifest(manifest: bytes) -> None`
  - `sealing.sealing_cert(ctx) -> bytes`, `sealing.seal(ctx, manifest) -> bytes`, `sealing.write_sealed(sealed, output: Path) -> None`
  - `sealing.backup_sealing_key(ctx) -> list[Path]`
  - CLI `talkub secret seal …`, `talkub secret backup-sealing-key`
  - conftest fixture `sealing_cert` → PEM bytes of a self-signed RSA certificate (needs openssl)

- [ ] **Step 1: Add the `sealing_cert` fixture to `tests/conftest.py`**

```python
@pytest.fixture(scope="session")
def sealing_cert(tmp_path_factory):
    """A self-signed certificate kubeseal can encrypt to (stands in for the controller's cert)."""
    import subprocess

    _require("openssl")
    directory = tmp_path_factory.mktemp("sealing")
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(directory / "key.pem"),
            "-out",
            str(directory / "cert.pem"),
            "-days",
            "1",
            "-subj",
            "/CN=sealed-secret",
        ],
        check=True,
        capture_output=True,
    )
    return (directory / "cert.pem").read_bytes()
```

- [ ] **Step 2: Write the failing tests**

`tests/test_sealing.py`:

```python
import base64

import pytest

from helpers import need
from talkub import sealing, yamlio
from talkub.context import Context
from talkub.errors import ValidationError
from talkub.fsutil import atomic_write

KEY_LIST = """apiVersion: v1
kind: List
items:
- apiVersion: v1
  kind: Secret
  type: kubernetes.io/tls
  metadata:
    name: sealed-secrets-keyabcde
    namespace: kube-system
    labels:
      sealedsecrets.bitnami.com/sealed-secrets-key: active
    resourceVersion: "123"
    uid: 0000-1111
    creationTimestamp: "2026-09-24T00:00:00Z"
    annotations:
      kubectl.kubernetes.io/last-applied-configuration: "{}"
  data:
    tls.crt: Y2VydA==
    tls.key: a2V5
"""


def test_secret_manifest_from_literals_and_files(tmp_path):
    token = tmp_path / "token.txt"
    token.write_bytes(b"s3cret\n")
    doc = yamlio.load(
        sealing.secret_manifest(
            "db", "app", ["user=admin"], [f"api={token}", str(token)]
        ).decode()
    )
    assert doc["kind"] == "Secret"
    assert doc["metadata"] == {"name": "db", "namespace": "app"}
    assert base64.b64decode(doc["data"]["user"]) == b"admin"
    assert base64.b64decode(doc["data"]["api"]) == b"s3cret\n"
    assert base64.b64decode(doc["data"]["token.txt"]) == b"s3cret\n"


@pytest.mark.parametrize(
    "literals, files, message",
    [
        ([], [], "at least one"),
        (["novalue"], [], "key=value"),
        (["bad key=x"], [], "invalid secret key"),
        (["a=1", "a=2"], [], "duplicate"),
        ([], ["/does/not/exist"], "cannot read"),
    ],
)
def test_secret_manifest_errors(literals, files, message):
    with pytest.raises(ValidationError) as exc:
        sealing.secret_manifest("db", "app", literals, files)
    assert message in str(exc.value) + (exc.value.hint or "")


@pytest.mark.parametrize(
    "text, message",
    [
        (
            "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: x, namespace: a}\n",
            "kind: Secret",
        ),
        ("apiVersion: v1\nkind: Secret\nmetadata: {name: x}\n", "namespace"),
        (
            "kind: Secret\nmetadata: {name: x, namespace: a}\n---\nkind: Secret\nmetadata: {name: y, namespace: a}\n",
            "exactly one",
        ),
        ("{not yaml", "YAML"),
    ],
)
def test_check_secret_manifest(text, message):
    with pytest.raises(ValidationError) as exc:
        sealing.check_secret_manifest(text.encode())
    assert message in str(exc.value) + (exc.value.hint or "")


pytestmark_keys = need("age", "age-keygen", "sops", "kubeseal")


@pytestmark_keys
def test_seal_falls_back_to_stored_cert(keyed_repo, sealing_cert, capfd):
    atomic_write(sealing.cert_path(keyed_repo), sealing_cert)
    sealed = sealing.seal(
        Context.open(keyed_repo),
        sealing.secret_manifest("db", "app", ["password=hunter2"], []),
    )
    doc = [d for d in yamlio.load_all(sealed.decode()) if d][0]
    assert doc["kind"] == "SealedSecret"
    assert doc["metadata"]["namespace"] == "app"
    assert b"hunter2" not in sealed
    assert "sealed-secrets.pem" in capfd.readouterr().err


@pytestmark_keys
def test_seal_prefers_live_cert(repo_with_configs, sealing_cert, spy):
    spy("kubectl")
    kubeseal = (
        spy("kubeseal").on("--fetch-cert", stdout=sealing_cert.decode()).real("--cert")
    )
    sealed = sealing.seal(
        Context.open(repo_with_configs),
        sealing.secret_manifest("db", "app", ["a=b"], []),
    )
    assert b"SealedSecret" in sealed
    fetch = kubeseal.calls[0]
    assert "--fetch-cert" in fetch["args"] and fetch["kubeconfig"] is True
    assert not sealing.cert_path(repo_with_configs).exists()


@pytestmark_keys
def test_seal_without_any_cert(keyed_repo):
    with pytest.raises(ValidationError) as exc:
        sealing.seal(
            Context.open(keyed_repo), sealing.secret_manifest("db", "app", ["a=b"], [])
        )
    assert "backup-sealing-key" in exc.value.hint


def test_write_sealed_requires_suffix(tmp_path):
    with pytest.raises(ValidationError):
        sealing.write_sealed(b"x", tmp_path / "db.yaml")
    sealing.write_sealed(b"x", tmp_path / "apps" / "db.sealed.yaml")
    assert (tmp_path / "apps" / "db.sealed.yaml").read_bytes() == b"x"


@need("age", "age-keygen", "sops")
def test_backup_sealing_key(repo_with_configs, sealing_cert, spy):
    spy("kubectl").on("get", "secret", stdout=KEY_LIST)
    spy("kubeseal").on("--fetch-cert", stdout=sealing_cert.decode())
    ctx = Context.open(repo_with_configs)
    written = sealing.backup_sealing_key(ctx)
    backup = sealing.key_backup_path(repo_with_configs)
    assert written == [backup, sealing.cert_path(repo_with_configs)]
    assert b"a2V5" not in backup.read_bytes()  # encrypted
    doc = yamlio.load(ctx.sops.decrypt(backup).decode())
    item = doc["items"][0]
    assert doc["kind"] == "List"
    assert item["data"]["tls.key"] == "a2V5"
    for field in ("resourceVersion", "uid", "creationTimestamp"):
        assert field not in item["metadata"]
    assert "kubectl.kubernetes.io/last-applied-configuration" not in item[
        "metadata"
    ].get("annotations", {})
    assert sealing.cert_path(repo_with_configs).read_bytes() == sealing_cert


@need("age", "age-keygen", "sops")
def test_backup_without_key_fails(repo_with_configs, spy):
    spy("kubectl").on("get", "secret", stdout="apiVersion: v1\nkind: List\nitems: []\n")
    spy("kubeseal")
    with pytest.raises(ValidationError) as exc:
        sealing.backup_sealing_key(Context.open(repo_with_configs))
    assert "controller" in exc.value.hint
```

`tests/test_cli_secret.py`:

```python
import pytest

from helpers import need
from talkub import sealing
from talkub.cli.main import main
from talkub.fsutil import atomic_write

pytestmark = need("age", "age-keygen", "sops", "kubeseal")


def _run(argv):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    return exc.value.code


def test_seal_from_literal(keyed_repo, sealing_cert, tmp_path):
    atomic_write(sealing.cert_path(keyed_repo), sealing_cert)
    out = keyed_repo.root / "gitops" / "manifests" / "app" / "db.sealed.yaml"
    code = _run(
        [
            "--repo",
            str(keyed_repo.root),
            "secret",
            "seal",
            "db",
            "-n",
            "app",
            "--from-literal",
            "password=hunter2",
            "-o",
            str(out),
        ]
    )
    assert code == 0
    assert b"kind: SealedSecret" in out.read_bytes()


def test_seal_refuses_manifest_inside_repo(keyed_repo, sealing_cert, capfd):
    atomic_write(sealing.cert_path(keyed_repo), sealing_cert)
    manifest = keyed_repo.root / "secret.yaml"
    manifest.write_text(
        "apiVersion: v1\nkind: Secret\nmetadata: {name: db, namespace: app}\ndata: {a: Yg==}\n"
    )
    code = _run(
        [
            "--repo",
            str(keyed_repo.root),
            "secret",
            "seal",
            str(manifest),
            "-o",
            str(keyed_repo.root / "db.sealed.yaml"),
        ]
    )
    assert code == 1
    assert "inside the cluster repo" in capfd.readouterr().err


def test_seal_manifest_from_outside(keyed_repo, sealing_cert, tmp_path):
    atomic_write(sealing.cert_path(keyed_repo), sealing_cert)
    manifest = tmp_path / "secret.yaml"
    manifest.write_text(
        "apiVersion: v1\nkind: Secret\nmetadata: {name: db, namespace: app}\ndata: {a: Yg==}\n"
    )
    out = tmp_path / "db.sealed.yaml"
    assert (
        _run(
            [
                "--repo",
                str(keyed_repo.root),
                "secret",
                "seal",
                str(manifest),
                "-o",
                str(out),
            ]
        )
        == 0
    )
    assert out.exists()


@pytest.mark.parametrize(
    "extra, message",
    [
        (["db", "--from-literal", "a=b", "-o", "x.sealed.yaml"], "--namespace"),
        (["db", "-n", "app", "--from-literal", "a=b", "-o", "x.yaml"], ".sealed.yaml"),
    ],
)
def test_seal_argument_errors(keyed_repo, sealing_cert, capfd, extra, message):
    atomic_write(sealing.cert_path(keyed_repo), sealing_cert)
    assert _run(["--repo", str(keyed_repo.root), "secret", "seal", *extra]) == 1
    assert message in capfd.readouterr().err
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_sealing.py tests/test_cli_secret.py -q`
Expected: `ImportError: cannot import name 'sealing'`.

- [ ] **Step 4: Implement `src/talkub/sealing.py`**

```python
"""Sealed Secrets: build Secrets in memory, seal them, back up the controller key."""

from __future__ import annotations

import base64
import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from ruamel.yaml.error import YAMLError

from talkub import yamlio
from talkub.context import Context
from talkub.ephemeral import private_file
from talkub.errors import TalkubError, ValidationError
from talkub.fsutil import atomic_write
from talkub.kube import kube_session
from talkub.repo import Repo
from talkub.runner import require_tool

CONTROLLER = [
    "--controller-name",
    "sealed-secrets-controller",
    "--controller-namespace",
    "kube-system",
]
KEY_LABEL = "sealedsecrets.bitnami.com/sealed-secrets-key"
RUNTIME_FIELDS = (
    "resourceVersion",
    "uid",
    "creationTimestamp",
    "managedFields",
    "ownerReferences",
    "generation",
    "selfLink",
)
_KEY = re.compile(r"[-._a-zA-Z0-9]+")


def cert_path(repo: Repo) -> Path:
    return repo.root / "bootstrap" / "sealed-secrets.pem"


def key_backup_path(repo: Repo) -> Path:
    return repo.root / "bootstrap" / "sealed-secrets-key.sops.yaml"


def _add(data: dict[str, str], key: str, value: bytes) -> None:
    if not _KEY.fullmatch(key):
        raise ValidationError(
            f"invalid secret key {key!r}", hint="use letters, digits, '-', '_' and '.'"
        )
    if key in data:
        raise ValidationError(f"duplicate secret key {key!r}")
    data[key] = base64.b64encode(value).decode()


def secret_manifest(
    name: str, namespace: str, literals: Sequence[str], files: Sequence[str]
) -> bytes:
    """A Secret manifest built in memory, so values never appear on a command line."""
    if not literals and not files:
        raise ValidationError("give at least one --from-literal or --from-file")
    data: dict[str, str] = {}
    for item in literals:
        key, sep, value = item.partition("=")
        if not sep:
            raise ValidationError(
                f"invalid --from-literal {item!r}", hint="use key=value"
            )
        _add(data, key, value.encode())
    for item in files:
        key, sep, path = item.partition("=")
        if not sep:
            key, path = Path(item).name, item
        try:
            content = Path(path).expanduser().read_bytes()
        except OSError as err:
            raise ValidationError(f"cannot read {path}: {err.strerror}") from None
        _add(data, key, content)
    manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "metadata": {"name": name, "namespace": namespace},
        "data": data,
    }
    return yamlio.dump(manifest).encode()


def check_secret_manifest(manifest: bytes) -> None:
    try:
        docs = [doc for doc in yamlio.load_all(manifest.decode()) if doc]
    except (YAMLError, UnicodeDecodeError):
        raise ValidationError("input is not valid YAML") from None
    if (
        len(docs) != 1
        or not isinstance(docs[0], Mapping)
        or docs[0].get("kind") != "Secret"
    ):
        raise ValidationError("input must be exactly one `kind: Secret` manifest")
    if not (docs[0].get("metadata") or {}).get("namespace"):
        raise ValidationError(
            "the Secret needs metadata.namespace",
            hint="sealed secrets are bound to their namespace",
        )


def sealing_cert(ctx: Context) -> bytes:
    """The controller's live certificate; bootstrap/sealed-secrets.pem when the cluster is unreachable."""
    reason = "the cluster is not bootstrapped yet"
    if ctx.repo.kubeconfig.is_file():
        try:
            with kube_session(ctx) as kube:
                proc = ctx.runner.run(
                    ["kubeseal", "--fetch-cert", *CONTROLLER], env=kube.env, check=False
                )
        except TalkubError as err:
            reason = str(err)
        else:
            if proc.returncode == 0 and b"BEGIN CERTIFICATE" in proc.stdout:
                return proc.stdout
            reason = (
                proc.stderr or b""
            ).decode().strip() or f"kubeseal exited with {proc.returncode}"
    fallback = cert_path(ctx.repo)
    if fallback.is_file():
        print(f"talkub: using {ctx.repo.rel(fallback)} ({reason})", file=sys.stderr)
        return fallback.read_bytes()
    raise ValidationError(
        "no sealing certificate available",
        hint=f"{reason}; {ctx.repo.rel(fallback)} is missing too — "
        "run `talkub secret backup-sealing-key` once the cluster is up",
    )


def seal(ctx: Context, manifest: bytes) -> bytes:
    require_tool("kubeseal")
    check_secret_manifest(manifest)
    cert = sealing_cert(ctx)
    with private_file("sealing-cert.pem", cert) as cert_file:
        # Offline sealing against the certificate: kubeseal must not talk to any cluster here.
        argv = ["kubeseal", "--format", "yaml", "--cert", str(cert_file)]
        return ctx.runner.run(
            argv, input=manifest, env={"KUBECONFIG": os.devnull}
        ).stdout


def write_sealed(sealed: bytes, output: Path) -> None:
    if not output.name.endswith(".sealed.yaml"):
        raise ValidationError(
            f"{output} must end with .sealed.yaml",
            hint="`talkub check` relies on the name",
        )
    atomic_write(output, sealed)


def backup_sealing_key(ctx: Context) -> list[Path]:
    """Store the controller key(s) SOPS-encrypted and the public certificate in bootstrap/."""
    require_tool("kubeseal")
    with kube_session(ctx) as kube:
        listing = kube.kubectl(
            ["get", "secret", "-n", "kube-system", "-l", KEY_LABEL, "-o", "yaml"]
        ).stdout
        cert = ctx.runner.run(
            ["kubeseal", "--fetch-cert", *CONTROLLER], env=kube.env
        ).stdout
    items = (yamlio.load(listing.decode()) or {}).get("items") or []
    if not items:
        raise ValidationError(
            "no sealed-secrets key found in kube-system",
            hint="is the sealed-secrets controller running?",
        )
    for item in items:
        metadata = item.get("metadata") or {}
        for key in RUNTIME_FIELDS:
            metadata.pop(key, None)
        (metadata.get("annotations") or {}).pop(
            "kubectl.kubernetes.io/last-applied-configuration", None
        )
    plain = yamlio.dump({"apiVersion": "v1", "kind": "List", "items": items}).encode()
    backup = key_backup_path(ctx.repo)
    atomic_write(
        backup,
        ctx.sops.encrypt(
            plain, filename=backup.name, public_key=ctx.keyring.public_key()
        ),
    )
    atomic_write(cert_path(ctx.repo), cert)
    return [backup, cert_path(ctx.repo)]
```

- [ ] **Step 5: Implement `src/talkub/cli/secret.py` and register it**

```python
"""secret seal, secret backup-sealing-key."""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from talkub import sealing
from talkub.errors import ValidationError
from talkub.repo import Repo

app = typer.Typer(
    help="Sealed Secrets for manifests under gitops/.", no_args_is_help=True
)


def _read_manifest(repo: Repo, source: str) -> bytes:
    if source == "-":
        return sys.stdin.buffer.read()
    path = Path(source).expanduser().resolve()
    if path.is_relative_to(repo.root):
        raise ValidationError(
            f"{source} is inside the cluster repo",
            hint="plaintext Secrets must not live in the repo; use --from-literal/--from-file "
            "or a manifest outside the repo",
        )
    try:
        return path.read_bytes()
    except OSError as err:
        raise ValidationError(f"cannot read {source}: {err.strerror}") from None


@app.command("seal")
def seal(
    ctx: typer.Context,
    source: str = typer.Argument(
        ...,
        help="Secret name (with --from-literal/--from-file), a Secret manifest file, or '-' for stdin.",
    ),
    namespace: str = typer.Option(
        None, "-n", "--namespace", help="Namespace (with --from-literal/--from-file)."
    ),
    literal: list[str] = typer.Option(
        None, "--from-literal", help="key=value (repeatable)."
    ),
    from_file: list[str] = typer.Option(
        None, "--from-file", help="[key=]path (repeatable)."
    ),
    output: Path = typer.Option(
        ..., "-o", "--output", help="Target file; must end with .sealed.yaml."
    ),
) -> None:
    """Seal a Secret for gitops/; the plaintext never touches the repo."""
    context = ctx.obj.context()
    if literal or from_file:
        if not namespace:
            raise ValidationError(
                "--namespace is required with --from-literal/--from-file"
            )
        manifest = sealing.secret_manifest(
            source, namespace, literal or [], from_file or []
        )
    else:
        if namespace:
            raise ValidationError(
                "--namespace only applies with --from-literal/--from-file",
                hint="the manifest's metadata.namespace is used",
            )
        manifest = _read_manifest(context.repo, source)
    if not output.name.endswith(".sealed.yaml"):
        raise ValidationError(
            f"{output} must end with .sealed.yaml",
            hint="`talkub check` relies on the name",
        )
    sealing.write_sealed(sealing.seal(context, manifest), output)
    print(f"wrote {output}")


@app.command("backup-sealing-key")
def backup_sealing_key(ctx: typer.Context) -> None:
    """Back up the controller key (SOPS) and certificate into bootstrap/."""
    context = ctx.obj.context()
    for path in sealing.backup_sealing_key(context):
        print(f"wrote {context.repo.rel(path)}")
    print(
        "commit bootstrap/ so a rebuilt cluster can still decrypt existing SealedSecrets"
    )
```

In `src/talkub/cli/main.py` extend the import line with `secret` (`from talkub.cli import admin, check, image, keys, secret, session, setup`) and add after `app.add_typer(image.app, name="image")`:

```python
app.add_typer(secret.app, name="secret")
```

- [ ] **Step 6: Run tests to verify they pass, then the full suite**

Run: `uv run pytest tests/test_sealing.py tests/test_cli_secret.py -q`, then `uv run pytest -q`
Expected: all passed.

- [ ] **Step 7: Commit**

```bash
git add src/talkub/sealing.py src/talkub/cli/secret.py src/talkub/cli/main.py tests/conftest.py tests/test_sealing.py tests/test_cli_secret.py
git commit -m "feat: secret seal and backup-sealing-key"
```

---

### Task 5: `bootstrap`

**Files:**
- Create: `src/talkub/bootstrap.py`, `src/talkub/cli/bootstrap.py`, `tests/test_bootstrap.py`
- Modify: `src/talkub/cli/main.py`, `src/talkub/access.py`

**Interfaces:**
- Consumes: `lifecycle.ordered_nodes`, `talos_session`/`TalosSession.run/call`, `kube_session`/`KubeSession.kubectl/helm`, `sealing.key_backup_path`, `sealing.backup_sealing_key`, `ephemeral.decrypted`, `atomic_write`, `repo.ChartSpec`.
- Produces: `bootstrap.BOOTSTRAP_HEALTH_TIMEOUT = "15m"`, `bootstrap.RELEASES` (key → (release, namespace, create_namespace)), `bootstrap.run(ctx) -> None`, CLI `talkub bootstrap`.

- [ ] **Step 1: Write the failing tests**

`tests/test_bootstrap.py`:

```python
import pytest

from helpers import need
from talkub import bootstrap, sealing
from talkub.context import Context
from talkub.errors import CommandFailed, ValidationError
from talkub.fsutil import atomic_write
from talkub.repo import ChartSpec

pytestmark = need("age", "age-keygen", "sops", "talosctl")

KUBECONFIG = "apiVersion: v1\nkind: Config\nclusters: []\n"
KEY_LIST = (
    "apiVersion: v1\nkind: List\nitems:\n- apiVersion: v1\n  kind: Secret\n"
    "  metadata: {name: key1, namespace: kube-system}\n  data: {tls.key: a2V5}\n"
)
CERT = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"


@pytest.fixture
def cluster(talos_repo, spy):
    """talos_repo prepared for bootstrap: charts, values, root app; no kubeconfig yet; spies."""
    settings = talos_repo.load_settings()
    settings.charts = {
        "sealed-secrets": ChartSpec(
            "https://bitnami.github.io/sealed-secrets", "sealed-secrets", "2.20.0"
        ),
        "argocd": ChartSpec(
            "https://argoproj.github.io/argo-helm", "argo-cd", "10.9.2"
        ),
    }
    talos_repo.write_settings(settings)
    for name in ("sealed-secrets.yaml", "argocd.yaml"):
        atomic_write(talos_repo.gitops_dir / "values" / name, b"{}\n")
    atomic_write(talos_repo.gitops_dir / "root.yaml", b"kind: Application\n")
    talos_repo.kubeconfig.unlink()
    spies = {
        "talosctl": spy("talosctl")
        .real("gen")
        .real("validate")
        .on("kubeconfig", stdout=KUBECONFIG),
        "kubectl": spy("kubectl").on("get", "secret", stdout=KEY_LIST),
        "helm": spy("helm"),
        "kubeseal": spy("kubeseal").on("--fetch-cert", stdout=CERT),
    }
    return talos_repo, spies


def test_fresh_bootstrap(cluster):
    repo, spies = cluster
    bootstrap.run(Context.open(repo))
    talos = spies["talosctl"].argvs
    assert [a[a.index("-e") + 2] for a in talos] == [
        "bootstrap",
        "health",
        "kubeconfig",
    ]
    assert "15m" in talos[1]
    ctx = Context.open(repo)
    assert ctx.sops.decrypt(repo.kubeconfig).decode() == KUBECONFIG
    helm = spies["helm"].argvs
    assert helm[0][:4] == ["upgrade", "--install", "sealed-secrets", "sealed-secrets"]
    assert "--create-namespace" not in helm[0]
    assert helm[1][:4] == ["upgrade", "--install", "argocd", "argo-cd"]
    assert "--create-namespace" in helm[1] and "argocd" in helm[1]
    assert all(
        call["kubeconfig"] and call["files"]["--values"] for call in spies["helm"].calls
    )
    kubectl = spies["kubectl"].argvs
    assert kubectl[0] == ["apply", "-f", str(repo.gitops_dir / "root.yaml")]
    assert kubectl[1][:2] == ["get", "secret"]
    assert sealing.key_backup_path(repo).exists()
    assert sealing.cert_path(repo).read_text() == CERT


def test_rerun_is_idempotent_and_restores_key_first(cluster):
    repo, spies = cluster
    bootstrap.run(Context.open(repo))
    kubeconfig_bytes = repo.kubeconfig.read_bytes()
    spies["talosctl"].rules.insert(
        0,
        {
            "match": ["bootstrap"],
            "stdout": "",
            "rc": 1,
            "stderr": "error: rpc error: code = AlreadyExists desc = etcd data directory is not empty",
        },
    )
    spies["talosctl"]._save()
    for fake in spies.values():
        fake.log.unlink(missing_ok=True)
    bootstrap.run(Context.open(repo))
    assert (
        repo.kubeconfig.read_bytes() == kubeconfig_bytes
    )  # unchanged content → not rewritten
    kubectl = spies["kubectl"].calls
    assert kubectl[0]["args"][:2] == ["apply", "-f"] and kubectl[0]["files"]["-f"]
    assert kubectl[0]["args"][2] != str(
        repo.gitops_dir / "root.yaml"
    )  # the decrypted key comes first
    assert not any(
        a[:2] == ["get", "secret"] for a in spies["kubectl"].argvs
    )  # no new backup
    assert len(spies["helm"].argvs) == 2


def test_health_failure_stops_before_kubeconfig(cluster):
    repo, spies = cluster
    spies["talosctl"].rules.insert(
        0, {"match": ["health"], "stdout": "", "stderr": "", "rc": 1}
    )
    spies["talosctl"]._save()
    with pytest.raises(CommandFailed) as exc:
        bootstrap.run(Context.open(repo))
    assert "healthy" in str(exc.value)
    assert not repo.kubeconfig.exists()
    assert spies["helm"].argvs == []


def test_bootstrap_error_is_reported(cluster):
    repo, spies = cluster
    spies["talosctl"].rules.insert(
        0,
        {"match": ["bootstrap"], "stdout": "", "stderr": "connection refused", "rc": 1},
    )
    spies["talosctl"]._save()
    with pytest.raises(CommandFailed) as exc:
        bootstrap.run(Context.open(repo))
    assert "connection refused" in exc.value.hint


def test_missing_chart_settings(cluster):
    repo, _ = cluster
    settings = repo.load_settings()
    del settings.charts["argocd"]
    repo.write_settings(settings)
    with pytest.raises(ValidationError) as exc:
        bootstrap.run(Context.open(repo))
    assert "argocd" in str(exc.value)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_bootstrap.py -q`
Expected: `ImportError: cannot import name 'bootstrap'`.

- [ ] **Step 3: Implement `src/talkub/bootstrap.py`**

```python
"""talkub bootstrap: etcd → health → kubeconfig → sealing key → Helm releases → root app."""

from __future__ import annotations

from talkub import sealing
from talkub.context import Context
from talkub.ephemeral import decrypted
from talkub.errors import CommandFailed, ValidationError
from talkub.fsutil import atomic_write
from talkub.kube import KubeSession, kube_session
from talkub.lifecycle import ordered_nodes
from talkub.repo import ChartSpec, Repo
from talkub.runner import require_tool
from talkub.talos.client import TalosSession, talos_session

BOOTSTRAP_HEALTH_TIMEOUT = "15m"
# chart key in .talkub.yaml -> (Helm release, namespace, create namespace)
RELEASES = {
    "sealed-secrets": ("sealed-secrets", "kube-system", False),
    "argocd": ("argocd", "argocd", True),
}


def _step(text: str) -> None:
    print(f"== {text}", flush=True)


def run(ctx: Context) -> None:
    for tool in ("talosctl", "kubectl", "helm", "kubeseal"):
        require_tool(tool)
    settings = ctx.repo.load_settings()
    missing = [key for key in RELEASES if key not in settings.charts]
    if missing:
        raise ValidationError(
            f"missing chart settings in .talkub.yaml: {', '.join(missing)}"
        )
    first = ordered_nodes(settings, [])[0]
    if settings.node(first).role != "controlplane":
        raise ValidationError("bootstrap needs a controlplane node")
    address = settings.mgmt_address(first)

    with talos_session(ctx) as talos:
        _step(f"bootstrapping etcd on {first} ({address})")
        _bootstrap_etcd(talos, address)
        _step("waiting for the cluster to become healthy")
        code = talos.call(
            address, ["health", "--wait-timeout", BOOTSTRAP_HEALTH_TIMEOUT]
        )
        if code != 0:
            raise CommandFailed(
                "the cluster did not become healthy",
                hint=f"talosctl health exited with {code}",
            )
        kubeconfig = talos.run(address, ["kubeconfig", "-", "--merge=false"]).stdout
    _store_kubeconfig(ctx, kubeconfig)

    backup = sealing.key_backup_path(ctx.repo)
    had_backup = backup.is_file()
    with kube_session(ctx) as kube:
        if had_backup:
            _step("restoring the sealed-secrets key")
            with decrypted(ctx.sops, {"sealing-key.yaml": backup}) as plain:
                kube.kubectl(["apply", "-f", str(plain["sealing-key.yaml"])])
        for key, (release, namespace, create) in RELEASES.items():
            _step(f"installing {release}")
            _helm_install(
                kube, ctx.repo, key, release, settings.charts[key], namespace, create
            )
        _step("applying gitops/root.yaml")
        kube.kubectl(["apply", "-f", str(ctx.repo.gitops_dir / "root.yaml")])

    if not had_backup:
        _step("backing up the new sealed-secrets key")
        sealing.backup_sealing_key(ctx)
    print("bootstrap complete — commit kubernetes/ and bootstrap/")


def _bootstrap_etcd(talos: TalosSession, address: str) -> None:
    proc = talos.run(address, ["bootstrap"], check=False)
    if proc.returncode == 0:
        return
    stderr = (proc.stderr or b"").decode()
    if "AlreadyExists" in stderr or "already bootstrapped" in stderr.lower():
        print("   etcd is already bootstrapped")
        return
    raise CommandFailed(
        "talosctl bootstrap failed",
        hint=stderr.strip() or f"exit code {proc.returncode}",
    )


def _store_kubeconfig(ctx: Context, kubeconfig: bytes) -> None:
    target = ctx.repo.kubeconfig
    if target.is_file() and ctx.sops.decrypt(target) == kubeconfig:
        print("   kubeconfig unchanged")
        return
    atomic_write(
        target,
        ctx.sops.encrypt(
            kubeconfig, filename=target.name, public_key=ctx.keyring.public_key()
        ),
    )
    print(f"   wrote {ctx.repo.rel(target)}")


def _helm_install(
    kube: KubeSession,
    repo: Repo,
    key: str,
    release: str,
    chart: ChartSpec,
    namespace: str,
    create: bool,
) -> None:
    values = repo.gitops_dir / "values" / f"{key}.yaml"
    if not values.is_file():
        raise ValidationError(f"{repo.rel(values)} does not exist")
    if chart.repo.startswith("oci://"):
        ref = [f"{chart.repo.rstrip('/')}/{chart.chart}"]
    else:
        ref = [chart.chart, "--repo", chart.repo]
    argv = [
        "upgrade",
        "--install",
        release,
        *ref,
        "--version",
        chart.version,
        "--namespace",
        namespace,
        "--values",
        str(values),
        "--wait",
        "--timeout",
        "10m",
    ]
    if create:
        argv.append("--create-namespace")
    kube.helm(argv)
```

- [ ] **Step 4: CLI and hint**

`src/talkub/cli/bootstrap.py`:

```python
"""bootstrap."""

from __future__ import annotations

import typer

from talkub import bootstrap as bootstrap_flow


def bootstrap(ctx: typer.Context) -> None:
    """Bootstrap etcd, fetch the kubeconfig, install Sealed Secrets and Argo CD, apply the root app.

    Safe to re-run after a failure; finished steps are detected and skipped.
    """
    bootstrap_flow.run(ctx.obj.context())
```

In `src/talkub/cli/main.py` add `from talkub.cli import bootstrap as bootstrap_cli` and register after `app.command("apply")(lifecycle_cli.apply)`:

```python
app.command("bootstrap")(bootstrap_cli.bootstrap)
```

In `src/talkub/access.py` set:

```python
KUBECONFIG_HINT = "it is created by `talkub bootstrap`"
```

(If a test asserts the old hint text, update it and say so in the report.)

- [ ] **Step 5: Run tests to verify they pass, then the full suite**

Run: `uv run pytest tests/test_bootstrap.py -q`, then `uv run pytest -q`
Expected: all passed.

- [ ] **Step 6: Commit**

```bash
git add src/talkub/bootstrap.py src/talkub/cli/bootstrap.py src/talkub/cli/main.py src/talkub/access.py tests/test_bootstrap.py
git commit -m "feat: idempotent cluster bootstrap"
```

---

### Task 6: `upgrade talos` / `upgrade k8s`

**Files:**
- Create: `src/talkub/upgrade.py`, `src/talkub/cli/upgrade.py`, `tests/test_upgrade.py`
- Modify: `src/talkub/cli/main.py`

**Interfaces:**
- Consumes: `lifecycle.ordered_nodes/ensure_images/is_bootstrapped/wait_healthy/Confirm`, `talos_session`, `inspect.server_version/schematic_id`, `Settings.installer_image/mgmt_address/node_platform/schematics`.
- Produces: `upgrade.talos(ctx, names, *, confirm) -> list[str]`, `upgrade.k8s(ctx, *, confirm) -> bool`, CLI `talkub upgrade talos [NODES…] [--yes]`, `talkub upgrade k8s [--yes]`.

- [ ] **Step 1: Write the failing tests**

`tests/test_upgrade.py`:

```python
import pytest

from helpers import DEFAULT_SCHEMATIC_ID, need
from talkub import upgrade
from talkub.context import Context
from talkub.errors import CommandFailed
from talkub.fsutil import atomic_write
from talkub.nodes import node_patch_path, render_node_patch
from talkub.repo import NodeSpec

pytestmark = need("age", "age-keygen", "sops", "talosctl")


def _version(tag):
    return f"Client:\n\tTag: v1.14.0\nServer:\n\tNODE: x\n\tTag: {tag}\n"


def _extensions(schematic):
    return '{"spec": {"metadata": {"name": "schematic", "version": "%s"}}}' % schematic


def _talosctl(spy, tag="v1.13.0", schematic=DEFAULT_SCHEMATIC_ID):
    return (
        spy("talosctl")
        .on("-n", "10.0.0.99", "version", rc=1, stderr="connection refused")
        .on("version", stdout=_version(tag))
        .on("get", "extensions", stdout=_extensions(schematic))
    )


def _add_worker(repo, name, ip):
    settings = repo.load_settings()
    settings.nodes[name] = NodeSpec(ip=ip, role="worker")
    repo.write_settings(settings)
    atomic_write(node_patch_path(repo, name), render_node_patch(name).encode())


def _actions(fake):
    return [a[a.index("-e") + 2] for a in fake.argvs]


def test_upgrade_outdated_node(talos_repo, spy):
    talosctl = _talosctl(spy, tag="v1.13.0")
    assert upgrade.talos(Context.open(talos_repo), [], confirm=lambda q: True) == [
        "cp-01"
    ]
    call = next(a for a in talosctl.argvs if "upgrade" in a)
    assert (
        call[call.index("--image") + 1]
        == f"factory.talos.dev/metal-installer/{DEFAULT_SCHEMATIC_ID}:v1.14.0"
    )
    assert "--wait" in call


def test_up_to_date_node_is_skipped(talos_repo, spy, capfd):
    talosctl = _talosctl(spy, tag="v1.14.0")
    assert upgrade.talos(Context.open(talos_repo), [], confirm=lambda q: True) == []
    assert "upgrade" not in _actions(talosctl)
    assert "up to date" in capfd.readouterr().out


def test_schematic_change_triggers_upgrade(talos_repo, spy):
    _talosctl(spy, tag="v1.14.0", schematic="0000")
    assert upgrade.talos(Context.open(talos_repo), [], confirm=lambda q: True) == [
        "cp-01"
    ]


def test_declined_upgrade(talos_repo, spy):
    talosctl = _talosctl(spy)
    assert upgrade.talos(Context.open(talos_repo), [], confirm=lambda q: False) == []
    assert "upgrade" not in _actions(talosctl)


def test_unreachable_node_stops_the_run(talos_repo, spy):
    _add_worker(talos_repo, "w-99", "10.0.0.99")
    _add_worker(talos_repo, "w-ok", "10.0.0.12")
    talosctl = _talosctl(spy)
    with pytest.raises(CommandFailed) as exc:
        upgrade.talos(Context.open(talos_repo), [], confirm=lambda q: True)
    assert "w-99" in str(exc.value)
    assert "already upgraded: cp-01" in exc.value.hint
    assert not any("10.0.0.12" in a for a in talosctl.argvs)


def test_health_wait_between_upgrades(talos_repo, spy):
    _add_worker(talos_repo, "w-01", "10.0.0.11")
    talosctl = _talosctl(spy)
    upgrade.talos(Context.open(talos_repo), [], confirm=lambda q: True)
    actions = _actions(talosctl)
    assert actions.index("health") == actions.index("upgrade") + 1


def test_k8s_dry_run_then_upgrade(talos_repo, spy):
    talosctl = spy("talosctl")
    assert upgrade.k8s(Context.open(talos_repo), confirm=lambda q: True) is True
    assert [a[a.index("-e") + 2 :] for a in talosctl.argvs] == [
        ["upgrade-k8s", "--to", "1.37.0", "--dry-run"],
        ["upgrade-k8s", "--to", "1.37.0"],
    ]


def test_k8s_declined_or_failing_dry_run(talos_repo, spy):
    talosctl = spy("talosctl")
    assert upgrade.k8s(Context.open(talos_repo), confirm=lambda q: False) is False
    assert len(talosctl.argvs) == 1
    talosctl.on("--dry-run", rc=1)
    with pytest.raises(CommandFailed):
        upgrade.k8s(Context.open(talos_repo), confirm=lambda q: True)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_upgrade.py -q`
Expected: `ImportError: cannot import name 'upgrade'`.

- [ ] **Step 3: Implement `src/talkub/upgrade.py`**

```python
"""upgrade talos / upgrade k8s towards the versions in .talkub.yaml."""

from __future__ import annotations

from collections.abc import Sequence

from talkub.context import Context
from talkub.errors import CommandFailed
from talkub.lifecycle import (
    Confirm,
    ensure_images,
    is_bootstrapped,
    ordered_nodes,
    wait_healthy,
)
from talkub.talos import inspect
from talkub.talos.client import talos_session


def _short(value: str | None) -> str:
    return value[:12] if value else "unknown"


def talos(ctx: Context, names: Sequence[str], *, confirm: Confirm) -> list[str]:
    """Upgrade nodes whose Talos version or schematic differs from the target, one at a time."""
    settings = ctx.repo.load_settings()
    order = ordered_nodes(settings, names)
    ensure_images(ctx, settings, order)
    rolling = is_bootstrapped(ctx)
    upgraded: list[str] = []
    with talos_session(ctx) as session:
        for index, name in enumerate(order):
            address = settings.mgmt_address(name)
            platform = settings.node_platform(name)
            image = settings.installer_image(platform)
            wanted = settings.schematics[platform].id
            try:
                version = session.run(address, ["version"], check=False)
                if version.returncode != 0:
                    raise CommandFailed(
                        f"cannot reach node '{name}' at {address}",
                        hint=(version.stderr or b"").decode().strip()
                        or f"exit code {version.returncode}",
                    )
                current = inspect.server_version(version.stdout.decode())
                schematic = inspect.schematic_id(
                    session.run(
                        address, ["get", "extensions", "-o", "json"], check=False
                    ).stdout.decode()
                )
                print(
                    f"== {name} ({address}): talos {current or 'unknown'} -> {settings.talos_version}, "
                    f"schematic {_short(schematic)} -> {_short(wanted)}",
                    flush=True,
                )
                if current == settings.talos_version and schematic == wanted:
                    print("   up to date")
                    continue
                if not confirm(f"upgrade {name} to {image}?"):
                    print(f"skipped {name}")
                    continue
                code = session.call(address, ["upgrade", "--image", image, "--wait"])
                if code != 0:
                    raise CommandFailed(
                        f"upgrade failed for node '{name}'",
                        hint=f"talosctl exited with {code}",
                    )
                upgraded.append(name)
                if rolling and index < len(order) - 1:
                    wait_healthy(session, address)
            except CommandFailed as err:
                progress = (
                    f"already upgraded: {', '.join(upgraded) or 'none'}; "
                    f"not touched: {', '.join(order[index + 1 :]) or 'none'}"
                )
                raise CommandFailed(str(err), hint=f"{err.hint}; {progress}") from None
    return upgraded


def k8s(ctx: Context, *, confirm: Confirm) -> bool:
    """Show `talosctl upgrade-k8s --dry-run`, then upgrade Kubernetes to the target version."""
    settings = ctx.repo.load_settings()
    first = ordered_nodes(settings, [])[0]
    address = settings.mgmt_address(first)
    target = ["upgrade-k8s", "--to", settings.kubernetes_version]
    with talos_session(ctx) as session:
        code = session.call(address, [*target, "--dry-run"])
        if code != 0:
            raise CommandFailed(
                "upgrade-k8s dry-run failed", hint=f"talosctl exited with {code}"
            )
        if not confirm(f"upgrade Kubernetes to {settings.kubernetes_version}?"):
            print("aborted")
            return False
        code = session.call(address, target)
        if code != 0:
            raise CommandFailed(
                "upgrade-k8s failed", hint=f"talosctl exited with {code}"
            )
    return True
```

- [ ] **Step 4: CLI**

`src/talkub/cli/upgrade.py`:

```python
"""upgrade talos, upgrade k8s."""

from __future__ import annotations

import typer

from talkub import upgrade as upgrade_flow

app = typer.Typer(
    help="Upgrade Talos or Kubernetes to the versions in .talkub.yaml.",
    no_args_is_help=True,
)
YES = typer.Option(False, "--yes", help="Do not ask before upgrading.")


def _confirm(yes: bool):
    def confirm(question: str) -> bool:
        return yes or typer.confirm(question, default=False)

    return confirm


@app.command("talos")
def talos(
    ctx: typer.Context,
    nodes: list[str] = typer.Argument(
        None, help="Nodes (default: all, control planes first)."
    ),
    yes: bool = YES,
) -> None:
    """Upgrade nodes whose Talos version or schematic differs (one at a time)."""
    upgrade_flow.talos(ctx.obj.context(), nodes or [], confirm=_confirm(yes))


@app.command("k8s")
def k8s(ctx: typer.Context, yes: bool = YES) -> None:
    """Upgrade Kubernetes (shows the talosctl dry-run first)."""
    if not upgrade_flow.k8s(ctx.obj.context(), confirm=_confirm(yes)):
        raise typer.Exit(1)
```

In `src/talkub/cli/main.py` add `from talkub.cli import upgrade as upgrade_cli` and `app.add_typer(upgrade_cli.app, name="upgrade")` next to the other registrations.

- [ ] **Step 5: Run tests to verify they pass, then the full suite**

Run: `uv run pytest tests/test_upgrade.py -q`, then `uv run pytest -q`
Expected: all passed.

- [ ] **Step 6: Commit**

```bash
git add src/talkub/upgrade.py src/talkub/cli/upgrade.py src/talkub/cli/main.py tests/test_upgrade.py
git commit -m "feat: upgrade talos (rolling) and upgrade k8s"
```

---

### Task 7: `status` and README

**Files:**
- Create: `src/talkub/status.py`, `src/talkub/cli/status.py`, `tests/test_status.py`
- Modify: `src/talkub/cli/main.py`, `README.md`

**Interfaces:**
- Consumes: `lifecycle.prepare/is_bootstrapped`, `talos_session`/`TalosSession.run`, `kube_session`/`KubeSession.kubectl`, `ephemeral.private_file`, `inspect.*`, `Settings`.
- Produces: `status.NodeStatus`, `status.StatusReport`, `status.collect(ctx) -> StatusReport`, `status.render(report) -> str`, CLI `talkub status`.

- [ ] **Step 1: Write the failing tests**

`tests/test_status.py`:

```python
from helpers import DEFAULT_SCHEMATIC_ID, need
from talkub import status
from talkub.cli.main import main
from talkub.context import Context
from talkub.fsutil import atomic_write
from talkub.nodes import node_patch_path, render_node_patch
from talkub.repo import NodeSpec

pytestmark = need("age", "age-keygen", "sops", "talosctl")

VERSION = "Client:\n\tTag: v1.14.0\nServer:\n\tNODE: x\n\tTag: v1.14.0\n"
EXT = (
    '{"spec": {"metadata": {"name": "schematic", "version": "%s"}}}'
    % DEFAULT_SCHEMATIC_ID
)
NODES = '{"items": [{"metadata": {"name": "cp-01"}, "status": {"nodeInfo": {"kubeletVersion": "v1.37.0"}}}]}'
APPS = '{"items": [{"status": {"sync": {"status": "Synced"}, "health": {"status": "Healthy"}}}]}'


def _spies(spy, drift="Dry run summary:\nNo changes.\n"):
    talosctl = (
        spy("talosctl")
        .real("gen")
        .real("validate")
        .on("-n", "10.0.0.99", rc=1, stderr="connection refused")
        .on("version", stdout=VERSION)
        .on("get", "extensions", stdout=EXT)
        .on("--dry-run", stdout=drift)
    )
    kubectl = (
        spy("kubectl")
        .on("get", "nodes", stdout=NODES)
        .on("get", "applications.argoproj.io", stdout=APPS)
    )
    return talosctl, kubectl


def test_healthy_cluster(talos_repo, spy):
    _spies(spy)
    report = status.collect(Context.open(talos_repo))
    [node] = report.nodes
    assert (node.reachable, node.talos, node.schematic, node.config, node.kubelet) == (
        True,
        "v1.14.0",
        DEFAULT_SCHEMATIC_ID,
        "in sync",
        "1.37.0",
    )
    assert report.applications == {"Synced/Healthy": 1}
    text = status.render(report)
    assert "cp-01" in text and "in sync" in text and "1 Synced/Healthy" in text
    assert "want" not in text


def test_drift_and_version_mismatch_are_marked(talos_repo, spy):
    _spies(spy, drift="Config diff:\n+x\n")
    report = status.collect(Context.open(talos_repo))
    report.nodes[0].talos = "v1.13.0"
    text = status.render(report)
    assert "changes" in text
    assert "v1.13.0 (want v1.14.0)" in text


def test_unreachable_node_and_unparseable_output(talos_repo, spy):
    settings = talos_repo.load_settings()
    settings.nodes["w-99"] = NodeSpec(ip="10.0.0.99", role="worker")
    talos_repo.write_settings(settings)
    atomic_write(
        node_patch_path(talos_repo, "w-99"), render_node_patch("w-99").encode()
    )
    talosctl, _ = _spies(spy)
    # after the 10.0.0.99 rule (index 2), before the regular version rule
    talosctl.rules.insert(
        3, {"match": ["version"], "stdout": "garbage", "stderr": "", "rc": 0}
    )
    talosctl._save()
    report = status.collect(Context.open(talos_repo))
    by_name = {n.name: n for n in report.nodes}
    assert by_name["w-99"].reachable is False
    assert by_name["cp-01"].talos is None
    text = status.render(report)
    assert "unreachable" in text and "unknown" in text


def test_not_bootstrapped(talos_repo, spy):
    talos_repo.kubeconfig.unlink()
    _, kubectl = _spies(spy)
    report = status.collect(Context.open(talos_repo))
    assert report.kube == "not bootstrapped"
    assert kubectl.argvs == []
    assert "not bootstrapped" in status.render(report)


def test_status_command(talos_repo, spy, capfd):
    _spies(spy)
    try:
        main(["--repo", str(talos_repo.root), "status"])
    except SystemExit as exc:
        assert exc.code == 0
    assert "cp-01" in capfd.readouterr().out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_status.py -q`
Expected: `ImportError: cannot import name 'status'`.

- [ ] **Step 3: Implement `src/talkub/status.py`**

```python
"""talkub status: target vs. actual per node, plus Argo CD applications."""

from __future__ import annotations

from dataclasses import dataclass

from talkub.context import Context
from talkub.ephemeral import private_file
from talkub.kube import kube_session
from talkub.lifecycle import is_bootstrapped, prepare
from talkub.repo import Settings
from talkub.talos import inspect
from talkub.talos.client import talos_session


@dataclass
class NodeStatus:
    name: str
    address: str
    reachable: bool
    talos: str | None = None
    schematic: str | None = None
    config: str = "unknown"
    kubelet: str | None = None


@dataclass
class StatusReport:
    settings: Settings
    nodes: list[NodeStatus]
    kube: str  # "ok" | "not bootstrapped" | "unavailable"
    applications: dict[str, int] | None = None


def collect(ctx: Context) -> StatusReport:
    settings, order, configs = prepare(ctx, [])
    nodes: list[NodeStatus] = []
    with talos_session(ctx) as session:
        for name in order:
            address = settings.mgmt_address(name)
            version = session.run(address, ["version"], check=False)
            if version.returncode != 0:
                nodes.append(NodeStatus(name, address, reachable=False))
                continue
            node = NodeStatus(
                name,
                address,
                reachable=True,
                talos=inspect.server_version(version.stdout.decode()),
            )
            extensions = session.run(
                address, ["get", "extensions", "-o", "json"], check=False
            )
            node.schematic = (
                inspect.schematic_id(extensions.stdout.decode())
                if extensions.returncode == 0
                else None
            )
            with private_file(f"{name}.yaml", configs[name]) as path:
                dry_run = session.run(
                    address,
                    ["apply-config", "--file", str(path), "--dry-run"],
                    check=False,
                )
            if dry_run.returncode == 0:
                node.config = inspect.config_drift(
                    (dry_run.stdout + (dry_run.stderr or b"")).decode()
                )
            nodes.append(node)

    if not is_bootstrapped(ctx):
        return StatusReport(settings, nodes, kube="not bootstrapped")
    with kube_session(ctx) as kube:
        kubelets = kube.kubectl(["get", "nodes", "-o", "json"], check=False)
        apps = kube.kubectl(
            ["get", "applications.argoproj.io", "-n", "argocd", "-o", "json"],
            check=False,
        )
    if kubelets.returncode != 0:
        return StatusReport(settings, nodes, kube="unavailable")
    versions = inspect.kubelet_versions(kubelets.stdout.decode())
    for node in nodes:
        node.kubelet = versions.get(node.name)
    applications = (
        inspect.application_summary(apps.stdout.decode())
        if apps.returncode == 0
        else None
    )
    return StatusReport(settings, nodes, kube="ok", applications=applications)


def _compare(actual: str | None, wanted: str | None, width: int | None = None) -> str:
    def cut(value: str | None) -> str | None:
        return value[:width] if value and width else value

    if actual is None:
        return "unknown"
    if wanted is not None and actual != wanted:
        return f"{cut(actual)} (want {cut(wanted)})"
    return cut(actual) or "unknown"


def render(report: StatusReport) -> str:
    settings = report.settings
    rows = [("NODE", "ADDRESS", "TALOS", "SCHEMATIC", "CONFIG", "KUBELET")]
    for node in report.nodes:
        if not node.reachable:
            rows.append((node.name, node.address, "unreachable", "-", "-", "-"))
            continue
        wanted = settings.schematics.get(settings.node_platform(node.name))
        rows.append(
            (
                node.name,
                node.address,
                _compare(node.talos, settings.talos_version),
                _compare(node.schematic, wanted.id if wanted else None, width=12),
                node.config,
                _compare(node.kubelet, settings.kubernetes_version)
                if node.kubelet
                else "-",
            )
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    lines = [
        f"cluster {settings.name}: talos {settings.talos_version}, kubernetes {settings.kubernetes_version}",
        "",
        *(
            "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
            for row in rows
        ),
        "",
    ]
    if report.kube != "ok":
        lines.append(f"kubernetes: {report.kube}")
    elif report.applications is None:
        lines.append("argocd: applications not available")
    elif not report.applications:
        lines.append("argocd: no applications")
    else:
        summary = ", ".join(
            f"{count} {state}" for state, count in sorted(report.applications.items())
        )
        lines.append(f"argocd: {summary}")
    return "\n".join(lines)
```

- [ ] **Step 4: CLI, registration and README**

`src/talkub/cli/status.py`:

```python
"""status."""

from __future__ import annotations

import typer

from talkub import status as status_flow


def status(ctx: typer.Context) -> None:
    """Show target vs. actual per node (Talos, schematic, config drift, kubelet) and Argo CD apps."""
    print(status_flow.render(status_flow.collect(ctx.obj.context())))
```

In `src/talkub/cli/main.py` add `from talkub.cli import status as status_cli` and `app.command("status")(status_cli.status)`.

In `README.md`, insert before `## Admin changes`:

````markdown
## Bootstrap, upgrades and status

    talkub bootstrap                 # etcd, kubeconfig, Sealed Secrets, Argo CD, root app (re-runnable)
    talkub status                    # target vs. actual per node + Argo CD sync/health
    talkub upgrade talos             # nodes whose Talos version or schematic differs, one at a time
    talkub upgrade k8s               # shows the talosctl dry-run first

## Sealed Secrets

    talkub secret seal db -n app --from-literal password=… -o gitops/manifests/app/db.sealed.yaml
    talkub secret seal ~/secret.yaml -o gitops/manifests/app/db.sealed.yaml   # manifest outside the repo
    talkub secret backup-sealing-key # done by bootstrap; commit bootstrap/ afterwards

Sealing uses the controller's live certificate and falls back to `bootstrap/sealed-secrets.pem`
when the cluster is unreachable.
````

- [ ] **Step 5: Run tests to verify they pass, then the full suite**

Run: `uv run pytest tests/test_status.py -q`, then `uv run pytest -q`
Expected: all passed.

- [ ] **Step 6: Commit**

```bash
git add src/talkub/status.py src/talkub/cli/status.py src/talkub/cli/main.py README.md tests/test_status.py
git commit -m "feat: status command"
```

---

## Out of scope for this plan (Phase 2c)

`init --tailscale`, `tailscale enable` (`talos/patches/tailscale.sops.yaml`, `firewall.yaml`, `mgmt`),
`sops edit` follow-ups (re-open the editor on invalid YAML, check `$EDITOR` before decrypting).
Smoke tests against a real node: talosctl output formats in `talos/inspect.py`, `apply-config --dry-run --mode try`.
