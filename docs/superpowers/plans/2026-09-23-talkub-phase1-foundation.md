# talkub Phase 1 (Foundation) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `talkub` CLI foundation: key management (age master key encrypted to admin identities), SOPS encryption, ephemeral decryption for `talosctl`/`kubectl`/`shell`, `init` of a new cluster repo, `admin`, `keys rotate`, `check` and `sops` commands.

**Architecture:** A `uv`-packaged Python CLI (Typer). All subprocesses go through one `Runner` that strips key material from child environments. A `Keyring` owns `keys/master.age` (the cluster master age key, encrypted to all admin recipients); `Sops` decrypts with that key passed only via `SOPS_AGE_KEY` to `sops`. `ephemeral.decrypted()` writes decrypted files into a private temp dir that is always removed. `scaffold.init_cluster()` creates the cluster repo from packaged templates.

**Tech Stack:** Python ≥ 3.12, uv, Typer ≥ 0.27, ruamel.yaml ≥ 0.18, pytest; external binaries `age`/`age-keygen` (v1.3), `sops` (3.13), `talosctl` (1.14), `helm` (4.x), optional `age-plugin-yubikey`, `age-plugin-se`.

**Spec:** `docs/superpowers/specs/2026-09-23-talkub-design.md` (read sections 2–8; section 9 defines this phase's scope).

## Global Constraints

- All code, identifiers, comments, docstrings and CLI output are in **English** (spec §6 "Sprache").
- `requires-python = ">=3.12"`; runtime deps only `typer>=0.27` and `ruamel.yaml>=0.18`.
- `runner.py` is the **only** module that calls `subprocess`. Arguments are always lists, never shell strings.
- `SOPS_AGE_KEY`, `SOPS_AGE_KEY_FILE`, `TALKUB_IDENTITY` are removed from every child environment unless a caller passes one back explicitly (only `Sops` does, and only `SOPS_AGE_KEY`).
- Master key resolution order: `SOPS_AGE_KEY`/`SOPS_AGE_KEY_FILE` → `TALKUB_IDENTITY` (`os.pathsep`-separated) → `$XDG_CONFIG_HOME/talkub/identities/` (default `~/.config/talkub/identities/`).
- No passphrase recovery. `admin remove` refuses to remove the last recipient; `<2` recipients is a warning.
- Plaintext secrets exist only in `tempfile.mkdtemp()` dirs (mode 0700), files mode 0600, removed in `finally`.
- Key-file writes use `fsutil.atomic_write` (temp file in the same dir + `os.replace`).
- File naming: `*.sops.yaml` = SOPS-encrypted; `.sops.yaml` is the SOPS config and is **not** a SOPS file.
- Talos ≥ 1.12 multi-document patches: hostname via `HostnameConfig` (`auto: "off"`), scheduling on control planes by deleting `KubeNodeConfig.taints` (`$patch: delete`).
- Chart defaults: `argocd` = `https://argoproj.github.io/argo-helm` / `argo-cd`; `sealed-secrets` = `https://bitnami.github.io/sealed-secrets` / `sealed-secrets`; versions resolved at `init` via `helm show chart`.
- Run tests with `uv run pytest`. Integration tests use the real binaries and are skipped when a binary is missing.
- Commit after every task; commit messages end with `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.

## Review Focus

1. **Hardware key prompts:** `age -d` with a YubiKey/Secure Enclave identity asks for touch/PIN on stderr — stderr must reach the terminal, not be captured (test in Task 4).
2. **Verbatim pass-through:** `talkub k exec pod -- sh` and `talkub t --help` must reach the tool unchanged, including `--` and `--help` (test in Task 11).
3. **Interrupted sessions:** SIGTERM or SIGHUP (terminal closed) during `talkub t`/`shell` must still remove decrypted files (test in Task 6).
4. **Failed `init`:** a failure halfway (e.g. no network for `helm show chart`) must not leave a half-built repo that blocks a retry (test in Task 9).
5. **`.sops.yaml` vs `*.sops.yaml`:** pathlib's `*.sops.yaml` glob also matches `.sops.yaml`; `check`/`rotate` must not treat the SOPS config as an encrypted file (test in Task 2).

---

## File Structure

```
pyproject.toml
README.md
.gitignore
src/talkub/
├── __init__.py            # __version__
├── errors.py              # TalkubError hierarchy (message + hint)
├── runner.py              # subprocess wrapper, env filtering, tool lookup
├── fsutil.py              # atomic_write
├── yamlio.py              # ruamel load/load_all/dump
├── repo.py                # Repo paths, Settings (.talkub.yaml), find_repo
├── recipients.py          # recipients.txt parse/render
├── identity.py            # identity files, env resolution, create_identity, own_recipients
├── crypto/__init__.py
├── crypto/age.py          # age-keygen / age encrypt / decrypt
├── crypto/sops.py         # Sops class, render_sops_config
├── keyring.py             # master key: create/add/remove/rotate/resolve
├── ephemeral.py           # private_tempdir, decrypted (signal-safe cleanup)
├── context.py             # Context (repo, runner, keyring, sops)
├── access.py              # talosctl / kubectl / shell with ephemeral credentials
├── charts.py              # DEFAULT_CHARTS, latest_version via helm
├── talos/__init__.py
├── talos/gen.py           # gen secrets, detect defaults, gen talosconfig
├── scaffold.py            # InitRequest, init_cluster, template rendering
├── checks.py              # Finding, run_checks
├── templates/
│   ├── node.yaml.tmpl
│   └── repo/              # mirrored into the cluster repo ("dot-" → ".", ".tmpl" rendered)
│       ├── dot-gitignore
│       ├── README.md.tmpl
│       ├── talos/patches/controlplane.yaml
│       └── gitops/{root.yaml.tmpl, apps/argocd.yaml.tmpl, apps/sealed-secrets.yaml.tmpl,
│                   values/argocd.yaml, values/sealed-secrets.yaml, manifests/dot-gitkeep}
└── cli/
    ├── __init__.py
    ├── main.py            # app, globals, error reporting, raw t/k dispatch
    ├── state.py           # State (repo path, debug) → Repo/Context
    ├── session.py         # shell + help stubs for talosctl/t, kubectl/k
    ├── setup.py           # identity new, init
    ├── admin.py           # admin add/remove/list
    ├── keys.py            # keys rotate
    ├── sops.py            # sops edit/decrypt
    └── check.py           # check
tests/
├── conftest.py            # env isolation + shared fixtures (lazy imports)
├── helpers.py             # need(), FakeRunner, Call, sample_settings()
└── test_*.py
```

---

### Task 1: Project skeleton, errors, runner

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `README.md`, `src/talkub/__init__.py`, `src/talkub/errors.py`, `src/talkub/runner.py`
- Create: `tests/conftest.py`, `tests/helpers.py`, `tests/test_runner.py`

**Interfaces:**
- Produces:
  - `talkub.errors`: `TalkubError(message: str, hint: str | None = None)` with `.hint`; subclasses `RepoNotFound`, `ValidationError`, `NoIdentity`, `DecryptFailed`, `ToolMissing`, `CommandFailed`.
  - `talkub.runner`: `SENSITIVE_ENV: tuple[str, ...]`, `child_env(extra: Mapping[str, str] | None = None) -> dict[str, str]`, `require_tool(name: str) -> str`, class `Runner` with `run(argv, *, input=None, env=None, cwd=None, check=True, passthrough_stderr=False) -> subprocess.CompletedProcess[bytes]` and `call(argv, *, env=None, cwd=None) -> int`.
  - `tests/helpers.py`: `need(*tools)`, `Call`, `FakeRunner`, `sample_settings()`.
  - `tests/conftest.py` fixtures: `isolated_env` (autouse), `fake_bin`, `software_identity`, `empty_repo`, `keyed_repo`, `repo_with_configs` (the last four import lazily and are first used in later tasks).

- [ ] **Step 1: Create project files**

`pyproject.toml`:

```toml
[project]
name = "talkub"
version = "0.1.0"
description = "Admin CLI for Talos Linux clusters managed with ArgoCD, SOPS/age and Sealed Secrets"
readme = "README.md"
requires-python = ">=3.12"
dependencies = [
    "typer>=0.27",
    "ruamel.yaml>=0.18",
]

[project.scripts]
talkub = "talkub.cli.main:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/talkub"]

[dependency-groups]
dev = ["pytest>=8"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["tests"]
```

`.gitignore`:

```
.venv/
__pycache__/
.pytest_cache/
dist/
*.egg-info/
```

`README.md`:

```markdown
# talkub

Admin CLI for a Talos Linux cluster repo (ArgoCD app-of-apps, SOPS/age, Sealed Secrets).
See `docs/superpowers/specs/2026-09-23-talkub-design.md`.
```

`src/talkub/__init__.py`:

```python
"""talkub - admin CLI for Talos cluster repos."""

__version__ = "0.1.0"
```

- [ ] **Step 2: Write test helpers and conftest**

`tests/helpers.py`:

```python
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

    def run(
        self,
        argv,
        *,
        input=None,
        env=None,
        cwd=None,
        check=True,
        passthrough_stderr=False,
    ):
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
```

`tests/conftest.py`:

```python
"""Shared fixtures. talkub imports are lazy so early tasks run before later modules exist."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from helpers import sample_settings


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    """Never touch the developer's real keys or kube/talos configs."""
    for var in (
        "SOPS_AGE_KEY",
        "SOPS_AGE_KEY_FILE",
        "TALKUB_IDENTITY",
        "TALOSCONFIG",
        "KUBECONFIG",
        "TALKUB_SESSION",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))


@pytest.fixture
def fake_bin(tmp_path, monkeypatch):
    """Install a fake executable (POSIX sh script) in front of PATH."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    def install(name: str, script: str) -> Path:
        path = bindir / name
        path.write_text("#!/bin/sh\n" + script)
        path.chmod(0o755)
        return path

    return install


def _require(*tools: str) -> None:
    missing = [tool for tool in tools if shutil.which(tool) is None]
    if missing:
        pytest.skip(f"missing tools: {', '.join(missing)}")


@pytest.fixture
def software_identity():
    """A software age identity in the isolated default identities dir: (path, public key)."""
    _require("age-keygen")
    from talkub.identity import create_identity, identities_dir
    from talkub.runner import Runner

    return create_identity(Runner(), "software", identities_dir(os.environ))


@pytest.fixture
def empty_repo(tmp_path):
    """A directory with a valid .talkub.yaml and nothing else."""
    from talkub.repo import Repo

    root = tmp_path / "cluster"
    root.mkdir()
    repo = Repo(root)
    repo.write_settings(sample_settings())
    return repo


@pytest.fixture
def keyed_repo(empty_repo, software_identity):
    """empty_repo plus a master key encrypted to software_identity (recipient 'me')."""
    _require("age")
    from talkub.keyring import Keyring
    from talkub.recipients import Recipient
    from talkub.runner import Runner

    Keyring(empty_repo, Runner()).create([Recipient("me", software_identity[1])])
    return empty_repo


@pytest.fixture
def repo_with_configs(keyed_repo):
    """keyed_repo plus SOPS-encrypted talosconfig and kubeconfig with marker content."""
    _require("sops")
    from talkub.context import Context
    from talkub.fsutil import atomic_write

    ctx = Context.open(keyed_repo)
    public = ctx.keyring.public_key()
    for path, text in (
        (keyed_repo.talosconfig, b"talos: marker\n"),
        (keyed_repo.kubeconfig, b"kube: marker\n"),
    ):
        atomic_write(path, ctx.sops.encrypt(text, filename=path.name, public_key=public))
    return keyed_repo
```

- [ ] **Step 3: Write the failing runner tests**

`tests/test_runner.py`:

```python
import sys

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


def test_run_child_does_not_inherit_key(monkeypatch):
    monkeypatch.setenv("SOPS_AGE_KEY", "secret")
    code = "import os; print(os.environ.get('SOPS_AGE_KEY', 'absent'))"
    assert Runner().run([sys.executable, "-c", code]).stdout.strip() == b"absent"


def test_call_returns_exit_code():
    assert Runner().call([sys.executable, "-c", "raise SystemExit(7)"]) == 7
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `uv run pytest tests/test_runner.py -v`
Expected: collection error `ModuleNotFoundError: No module named 'talkub.errors'` (uv creates the venv on first run).

- [ ] **Step 5: Implement errors and runner**

`src/talkub/errors.py`:

```python
"""Errors that are reported to the user without a traceback."""


class TalkubError(Exception):
    """Base class. `str(err)` is the user-facing message, `hint` an optional next step."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


class RepoNotFound(TalkubError):
    """No cluster repo (.talkub.yaml) could be located."""


class ValidationError(TalkubError):
    """Invalid input, settings or repo state."""


class NoIdentity(TalkubError):
    """No age identity or master key is available."""


class DecryptFailed(TalkubError):
    """age or sops could not decrypt a file."""


class ToolMissing(TalkubError):
    """A required external binary is not installed."""


class CommandFailed(TalkubError):
    """An external command exited with a non-zero status."""
```

`src/talkub/runner.py`:

```python
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
    "helm": "brew install helm",
    "kubectl": "brew install kubectl",
    "kubeseal": "brew install kubeseal",
    "sops": "brew install sops",
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
    ) -> subprocess.CompletedProcess[bytes]:
        """Run non-interactively and capture stdout.

        `passthrough_stderr` lets prompts (e.g. "touch your YubiKey") reach the terminal.
        """
        require_tool(argv[0])
        proc = subprocess.run(
            list(argv),
            input=input,
            stdout=subprocess.PIPE,
            stderr=None if passthrough_stderr else subprocess.PIPE,
            env=child_env(env),
            cwd=cwd,
        )
        if check and proc.returncode != 0:
            stderr = (proc.stderr or b"").decode(errors="replace").strip()
            raise CommandFailed(
                f"{Path(argv[0]).name} exited with {proc.returncode}",
                hint=stderr or None,
            )
        return proc

    def call(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> int:
        """Run interactively with inherited stdio and return the exit code."""
        require_tool(argv[0])
        proc = subprocess.Popen(list(argv), env=child_env(env), cwd=cwd)
        while True:
            try:
                return proc.wait()
            except KeyboardInterrupt:
                # The child got SIGINT too (same process group); let it decide when to exit.
                continue
            except BaseException:
                proc.terminate()
                proc.wait()
                raise
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_runner.py -v`
Expected: 8 passed.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock .gitignore README.md src tests
git commit -m "feat: project skeleton, error types and subprocess runner

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: atomic writes, YAML I/O, repo and settings

**Files:**
- Create: `src/talkub/fsutil.py`, `src/talkub/yamlio.py`, `src/talkub/repo.py`
- Test: `tests/test_fsutil.py`, `tests/test_repo.py`

**Interfaces:**
- Consumes: `talkub.errors.RepoNotFound`, `ValidationError`.
- Produces:
  - `fsutil.atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None`
  - `yamlio.load(text: str) -> Any`, `yamlio.load_all(text: str) -> list[Any]`, `yamlio.dump(data: Any) -> str`
  - `repo.MARKER = ".talkub.yaml"`, `repo.ROLES = ("controlplane", "worker")`
  - `repo.NodeSpec(ip: str, role: str = "controlplane")` (frozen)
  - `repo.ChartSpec(repo: str, chart: str, version: str)` (frozen)
  - `repo.Settings(name, endpoint, talos_version, kubernetes_version, install_image, nodes: dict[str, NodeSpec] = {}, charts: dict[str, ChartSpec] = {})` with `to_dict()` / `Settings.from_dict(data)`
  - `repo.Repo(root: Path)` (frozen) with properties `settings_file, sops_config, keys_dir, recipients_file, master_age, master_next_age, master_pub, talos_dir, talos_secrets, talosconfig, kubeconfig, gitops_dir` and methods `rel(path) -> str`, `load_settings() -> Settings`, `write_settings(settings) -> None`, `sops_files() -> list[Path]`
  - `repo.find_repo(start: Path, explicit: Path | None = None) -> Repo`

- [ ] **Step 1: Write the failing tests**

`tests/test_fsutil.py`:

```python
import os

import pytest

from talkub.fsutil import atomic_write


def test_atomic_write_creates_parents_and_sets_mode(tmp_path):
    target = tmp_path / "a" / "b" / "file.txt"
    atomic_write(target, b"hello", mode=0o600)
    assert target.read_bytes() == b"hello"
    assert target.stat().st_mode & 0o777 == 0o600


def test_atomic_write_keeps_original_on_failure(tmp_path, monkeypatch):
    target = tmp_path / "file.txt"
    target.write_bytes(b"original")

    def broken_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(OSError):
        atomic_write(target, b"new")
    assert target.read_bytes() == b"original"
    assert [p.name for p in tmp_path.iterdir()] == ["file.txt"]
```

`tests/test_repo.py`:

```python
import pytest

from helpers import sample_settings
from talkub.errors import RepoNotFound, ValidationError
from talkub.repo import MARKER, ChartSpec, NodeSpec, Repo, Settings, find_repo


def test_settings_roundtrip(tmp_path):
    settings = sample_settings()
    settings.charts["argocd"] = ChartSpec("https://argoproj.github.io/argo-helm", "argo-cd", "10.9.2")
    repo = Repo(tmp_path)
    repo.write_settings(settings)
    assert repo.load_settings() == settings
    assert "talosVersion: v1.14.0" in repo.settings_file.read_text()


def test_find_repo_searches_upwards(tmp_path):
    Repo(tmp_path).write_settings(sample_settings())
    nested = tmp_path / "gitops" / "apps"
    nested.mkdir(parents=True)
    assert find_repo(nested).root == tmp_path.resolve()


def test_find_repo_explicit_path_must_be_repo(tmp_path):
    with pytest.raises(RepoNotFound):
        find_repo(tmp_path, explicit=tmp_path)


def test_find_repo_not_found(tmp_path):
    with pytest.raises(RepoNotFound) as exc:
        find_repo(tmp_path)
    assert "talkub init" in exc.value.hint


def _data(**overrides):
    data = sample_settings().to_dict()
    data.update(overrides)
    return data


def test_invalid_role_rejected():
    with pytest.raises(ValidationError) as exc:
        Settings.from_dict(_data(nodes={"n1": {"ip": "10.0.0.2", "role": "master"}}))
    assert "role" in str(exc.value)


def test_endpoint_must_be_https():
    with pytest.raises(ValidationError):
        Settings.from_dict(_data(endpoint="10.0.0.10:6443"))


def test_unquoted_numeric_version_gives_hint():
    with pytest.raises(ValidationError) as exc:
        Settings.from_dict(_data(kubernetesVersion=1.37))
    assert "quote" in exc.value.hint


def test_invalid_yaml_reported(tmp_path):
    (tmp_path / MARKER).write_text("name: [unclosed\n")
    with pytest.raises(ValidationError):
        Repo(tmp_path).load_settings()


def test_sops_files_excludes_sops_config_and_git(tmp_path):
    repo = Repo(tmp_path)
    (tmp_path / ".sops.yaml").write_text("creation_rules: []\n")
    (tmp_path / "talos").mkdir()
    (tmp_path / "talos" / "secrets.sops.yaml").write_text("a: b\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "x.sops.yaml").write_text("a: b\n")
    assert repo.sops_files() == [tmp_path / "talos" / "secrets.sops.yaml"]


def test_node_defaults_to_controlplane():
    settings = Settings.from_dict(_data(nodes={"cp-01": {"ip": "10.0.0.10"}}))
    assert settings.nodes["cp-01"] == NodeSpec(ip="10.0.0.10", role="controlplane")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_fsutil.py tests/test_repo.py -v`
Expected: `ModuleNotFoundError: No module named 'talkub.fsutil'` / `'talkub.repo'`.

- [ ] **Step 3: Implement**

`src/talkub/fsutil.py`:

```python
"""Filesystem helpers."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    """Write via a temp file in the same directory and os.replace().

    Readers see either the old or the new content, never a partial file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
```

`src/talkub/yamlio.py`:

```python
"""YAML helpers based on ruamel.yaml (round-trip mode keeps comments)."""

from __future__ import annotations

from io import StringIO
from typing import Any

from ruamel.yaml import YAML


def _yaml() -> YAML:
    yaml = YAML()
    yaml.default_flow_style = False
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def load(text: str) -> Any:
    return _yaml().load(text)


def load_all(text: str) -> list[Any]:
    return list(_yaml().load_all(text))


def dump(data: Any) -> str:
    buffer = StringIO()
    _yaml().dump(data, buffer)
    return buffer.getvalue()
```

`src/talkub/repo.py`:

```python
"""Locating a cluster repo and reading/writing its settings (.talkub.yaml)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ruamel.yaml.error import YAMLError

from talkub import yamlio
from talkub.errors import RepoNotFound, ValidationError
from talkub.fsutil import atomic_write

MARKER = ".talkub.yaml"
ROLES = ("controlplane", "worker")


@dataclass(frozen=True)
class NodeSpec:
    ip: str
    role: str = "controlplane"


@dataclass(frozen=True)
class ChartSpec:
    repo: str
    chart: str
    version: str


@dataclass
class Settings:
    name: str
    endpoint: str
    talos_version: str
    kubernetes_version: str
    install_image: str
    nodes: dict[str, NodeSpec] = field(default_factory=dict)
    charts: dict[str, ChartSpec] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "endpoint": self.endpoint,
            "talosVersion": self.talos_version,
            "kubernetesVersion": self.kubernetes_version,
            "installImage": self.install_image,
            "nodes": {name: {"ip": n.ip, "role": n.role} for name, n in self.nodes.items()},
            "charts": {
                name: {"repo": c.repo, "chart": c.chart, "version": c.version} for name, c in self.charts.items()
            },
        }

    @classmethod
    def from_dict(cls, data: object) -> Settings:
        if not isinstance(data, Mapping):
            raise ValidationError(f"{MARKER} must be a mapping")

        def text(key: str) -> str:
            value = data.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                raise ValidationError(
                    f"{MARKER}: '{key}' must be a string",
                    hint=f'quote it, e.g. {key}: "{value}"',
                )
            if not isinstance(value, str) or not value.strip():
                raise ValidationError(f"{MARKER}: '{key}' must be a non-empty string")
            return value.strip()

        endpoint = text("endpoint")
        if not endpoint.startswith("https://"):
            raise ValidationError(
                f"{MARKER}: 'endpoint' must start with https://",
                hint="e.g. https://10.0.0.10:6443",
            )

        nodes: dict[str, NodeSpec] = {}
        for name, spec in (data.get("nodes") or {}).items():
            if not isinstance(spec, Mapping) or not isinstance(spec.get("ip"), str):
                raise ValidationError(f"{MARKER}: node '{name}' needs an 'ip'")
            role = spec.get("role", "controlplane")
            if role not in ROLES:
                raise ValidationError(
                    f"{MARKER}: node '{name}' has invalid role '{role}'",
                    hint=f"use one of: {', '.join(ROLES)}",
                )
            nodes[str(name)] = NodeSpec(ip=spec["ip"], role=role)

        charts: dict[str, ChartSpec] = {}
        for name, spec in (data.get("charts") or {}).items():
            try:
                charts[str(name)] = ChartSpec(
                    repo=str(spec["repo"]),
                    chart=str(spec["chart"]),
                    version=str(spec["version"]),
                )
            except (KeyError, TypeError):
                raise ValidationError(f"{MARKER}: chart '{name}' needs repo, chart and version") from None

        return cls(
            name=text("name"),
            endpoint=endpoint,
            talos_version=text("talosVersion"),
            kubernetes_version=text("kubernetesVersion"),
            install_image=text("installImage"),
            nodes=nodes,
            charts=charts,
        )


@dataclass(frozen=True)
class Repo:
    root: Path

    @property
    def settings_file(self) -> Path:
        return self.root / MARKER

    @property
    def sops_config(self) -> Path:
        return self.root / ".sops.yaml"

    @property
    def keys_dir(self) -> Path:
        return self.root / "keys"

    @property
    def recipients_file(self) -> Path:
        return self.keys_dir / "recipients.txt"

    @property
    def master_age(self) -> Path:
        return self.keys_dir / "master.age"

    @property
    def master_next_age(self) -> Path:
        return self.keys_dir / "master.next.age"

    @property
    def master_pub(self) -> Path:
        return self.keys_dir / "master.pub"

    @property
    def talos_dir(self) -> Path:
        return self.root / "talos"

    @property
    def talos_secrets(self) -> Path:
        return self.talos_dir / "secrets.sops.yaml"

    @property
    def talosconfig(self) -> Path:
        return self.talos_dir / "talosconfig.sops.yaml"

    @property
    def kubeconfig(self) -> Path:
        return self.root / "kubernetes" / "kubeconfig.sops.yaml"

    @property
    def gitops_dir(self) -> Path:
        return self.root / "gitops"

    def rel(self, path: Path) -> str:
        return str(path.relative_to(self.root))

    def load_settings(self) -> Settings:
        try:
            data = yamlio.load(self.settings_file.read_text())
        except FileNotFoundError:
            raise RepoNotFound(f"{self.settings_file} does not exist") from None
        except YAMLError as err:
            raise ValidationError(f"{MARKER} is not valid YAML: {err}") from err
        return Settings.from_dict(data)

    def write_settings(self, settings: Settings) -> None:
        atomic_write(self.settings_file, yamlio.dump(settings.to_dict()).encode())

    def sops_files(self) -> list[Path]:
        """All SOPS-encrypted files. `.sops.yaml` (the SOPS config) is not one of them."""
        return sorted(
            path
            for path in self.root.rglob("*.sops.yaml")
            if path.name != ".sops.yaml" and ".git" not in path.relative_to(self.root).parts
        )


def find_repo(start: Path, explicit: Path | None = None) -> Repo:
    if explicit is not None:
        root = explicit.expanduser().resolve()
        if not (root / MARKER).is_file():
            raise RepoNotFound(f"{root} is not a talkub repo (no {MARKER})")
        return Repo(root)
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / MARKER).is_file():
            return Repo(candidate)
    raise RepoNotFound(
        f"no {MARKER} found in {current} or any parent directory",
        hint="cd into a cluster repo, pass --repo, or create one with `talkub init`",
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_fsutil.py tests/test_repo.py -v`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add src/talkub/fsutil.py src/talkub/yamlio.py src/talkub/repo.py tests/test_fsutil.py tests/test_repo.py
git commit -m "feat: repo discovery, settings model and atomic writes

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: recipients file and identity handling

**Files:**
- Create: `src/talkub/recipients.py`, `src/talkub/identity.py`
- Test: `tests/test_recipients.py`, `tests/test_identity.py`

**Interfaces:**
- Consumes: `Runner`, `atomic_write`, errors.
- Produces:
  - `recipients.Recipient(name: str, key: str)` (frozen), `recipients.validate_key(key) -> str`, `recipients.validate_name(name) -> str`, `recipients.parse(text) -> list[Recipient]`, `recipients.render(items: Sequence[Recipient]) -> str`
  - `identity.KINDS = ("software", "yubikey", "se")`
  - `identity.identities_dir(env: Mapping[str, str]) -> Path`
  - `identity.master_key_from_env(env) -> str | None`
  - `identity.identity_files(env) -> list[Path]`
  - `identity.recipient_of(path: Path) -> str | None`
  - `identity.create_identity(runner, kind: str, directory: Path, name: str | None = None) -> tuple[Path, str]`
  - `identity.own_recipients(env, user: str) -> list[Recipient]`

- [ ] **Step 1: Write the failing tests**

`tests/test_recipients.py`:

```python
import pytest

from talkub.errors import ValidationError
from talkub.recipients import Recipient, parse, render

KEY_A = "age1qk2vu7xxezxcfhyhasnx8c7z5j479xwevu5tqq8tc2fjqjzdegjqvepymq"
KEY_B = "age1yubikey1qgw6x0m7l9t6tg3hvc8d0x3q7tnfq8m5h4k6s2r3v4w5x6y7z8a9bcdefg"


def test_render_parse_roundtrip():
    items = [Recipient("alice", KEY_A), Recipient("bob-yubikey", KEY_B)]
    text = render(items)
    assert text.splitlines()[1:] == [
        "# name: alice",
        KEY_A,
        "# name: bob-yubikey",
        KEY_B,
    ]
    assert parse(text) == items


def test_parse_ignores_comments_and_names_unnamed_keys():
    text = f"# some comment\n\n{KEY_A}\n"
    assert parse(text) == [Recipient("unnamed-3", KEY_A)]


def test_parse_rejects_non_age_line():
    with pytest.raises(ValidationError):
        parse("ssh-ed25519 AAAA\n")


def test_invalid_name_rejected():
    with pytest.raises(ValidationError):
        render([Recipient("with space", KEY_A)])
```

`tests/test_identity.py`:

```python
import os

import pytest

from helpers import FakeRunner, need
from talkub.errors import NoIdentity
from talkub.identity import (
    create_identity,
    identities_dir,
    identity_files,
    master_key_from_env,
    own_recipients,
    recipient_of,
)
from talkub.runner import Runner

SECRET = "AGE-SECRET-KEY-133FP6CADJCE6M9H6AUXY3NLXNX73NSSLQS0CHGVLPSVZKK87V80SPY0R56"


def test_identities_dir_uses_xdg(tmp_path):
    assert identities_dir({"XDG_CONFIG_HOME": str(tmp_path)}) == tmp_path / "talkub" / "identities"


def test_master_key_from_env_direct():
    assert master_key_from_env({"SOPS_AGE_KEY": f"# comment\n{SECRET}\n"}) == SECRET


def test_master_key_from_env_file(tmp_path):
    keyfile = tmp_path / "k.txt"
    keyfile.write_text(f"# created\n{SECRET}\n")
    assert master_key_from_env({"SOPS_AGE_KEY_FILE": str(keyfile)}) == SECRET


def test_master_key_from_env_file_missing(tmp_path):
    with pytest.raises(NoIdentity):
        master_key_from_env({"SOPS_AGE_KEY_FILE": str(tmp_path / "nope")})


def test_master_key_from_env_absent():
    assert master_key_from_env({}) is None


def test_identity_files_prefers_talkub_identity(tmp_path):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("x")
    b.write_text("x")
    env = {
        "TALKUB_IDENTITY": f"{a}{os.pathsep}{b}",
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
    }
    assert identity_files(env) == [a, b]


def test_identity_files_missing_talkub_identity(tmp_path):
    with pytest.raises(NoIdentity):
        identity_files({"TALKUB_IDENTITY": str(tmp_path / "missing.txt")})


def test_identity_files_default_dir_skips_hidden(tmp_path):
    directory = identities_dir({"XDG_CONFIG_HOME": str(tmp_path)})
    directory.mkdir(parents=True)
    (directory / "yubikey.txt").write_text("x")
    (directory / ".DS_Store").write_text("x")
    assert identity_files({"XDG_CONFIG_HOME": str(tmp_path)}) == [directory / "yubikey.txt"]


def test_recipient_of_formats(tmp_path):
    keygen = tmp_path / "sw.txt"
    keygen.write_text("# created: x\n# public key: age1abc\nAGE-SECRET-KEY-1X\n")
    yubikey = tmp_path / "yk.txt"
    yubikey.write_text("#       Serial: 1, Slot: 1\n#    Recipient: age1yubikey1xyz\nAGE-PLUGIN-YUBIKEY-1X\n")
    assert recipient_of(keygen) == "age1abc"
    assert recipient_of(yubikey) == "age1yubikey1xyz"


@need("age-keygen")
def test_create_software_identity(tmp_path):
    path, public = create_identity(Runner(), "software", tmp_path / "ids")
    assert path.name == "software.txt"
    assert public.startswith("age1")
    assert path.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "ids").stat().st_mode & 0o777 == 0o700


def test_create_yubikey_identity_uses_plugin(tmp_path):
    runner = FakeRunner(outputs={"age-plugin-yubikey": b"#    Recipient: age1yubikey1xyz\nAGE-PLUGIN-YUBIKEY-1X\n"})
    path, public = create_identity(runner, "yubikey", tmp_path)
    assert public == "age1yubikey1xyz"
    assert [c.argv for c in runner.calls] == [
        ["age-plugin-yubikey", "--generate"],
        ["age-plugin-yubikey", "--identity"],
    ]


def test_create_secure_enclave_identity(tmp_path):
    def write_identity(argv):
        out = argv[argv.index("-o") + 1]
        with open(out, "w") as fh:
            fh.write("# public key: age1se1abc\nAGE-PLUGIN-SE-1X\n")

    runner = FakeRunner(on_run=write_identity)
    _, public = create_identity(runner, "se", tmp_path)
    assert public == "age1se1abc"
    assert runner.calls[0].argv[:2] == ["age-plugin-se", "keygen"]


def test_own_recipients_requires_identity(tmp_path):
    with pytest.raises(NoIdentity) as exc:
        own_recipients({"XDG_CONFIG_HOME": str(tmp_path)}, user="jam")
    assert "identity new" in exc.value.hint


def test_own_recipients_names_by_user_and_file(tmp_path):
    directory = identities_dir({"XDG_CONFIG_HOME": str(tmp_path)})
    directory.mkdir(parents=True)
    (directory / "yubikey.txt").write_text("#    Recipient: age1yubikey1xyz\n")
    result = own_recipients({"XDG_CONFIG_HOME": str(tmp_path)}, user="jan.mock")
    assert [(r.name, r.key) for r in result] == [("jan.mock-yubikey", "age1yubikey1xyz")]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_recipients.py tests/test_identity.py -v`
Expected: `ModuleNotFoundError` for `talkub.recipients` / `talkub.identity`.

- [ ] **Step 3: Implement**

`src/talkub/recipients.py`:

```python
"""keys/recipients.txt: the admins' age public keys.

Format (compatible with `age -R`, which ignores comment lines):

    # name: alice
    age1...
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from talkub.errors import ValidationError

HEADER = "# talkub admin recipients - managed by `talkub admin`, readable by `age -R`"
_NAME_LINE = re.compile(r"^#\s*name:\s*(\S+)\s*$")
_KEY = re.compile(r"age1[0-9a-z]+")
_NAME = re.compile(r"[A-Za-z0-9._-]+")


@dataclass(frozen=True)
class Recipient:
    name: str
    key: str


def validate_key(key: str) -> str:
    key = key.strip()
    if not _KEY.fullmatch(key):
        raise ValidationError(f"not an age recipient: {key!r}", hint="age public keys start with 'age1'")
    return key


def validate_name(name: str) -> str:
    if not _NAME.fullmatch(name):
        raise ValidationError(
            f"invalid recipient name {name!r}",
            hint="use letters, digits, '.', '_' or '-'",
        )
    return name


def parse(text: str) -> list[Recipient]:
    result: list[Recipient] = []
    pending: str | None = None
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if match := _NAME_LINE.match(line):
            pending = match.group(1)
            continue
        if line.startswith("#"):
            continue
        result.append(Recipient(pending or f"unnamed-{lineno}", validate_key(line)))
        pending = None
    return result


def render(items: Sequence[Recipient]) -> str:
    lines = [HEADER]
    for item in items:
        lines += [f"# name: {validate_name(item.name)}", validate_key(item.key)]
    return "\n".join(lines) + "\n"
```

`src/talkub/identity.py`:

```python
"""Personal age identities and master key lookup from the environment."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

from talkub.errors import CommandFailed, NoIdentity, ValidationError
from talkub.fsutil import atomic_write
from talkub.recipients import Recipient
from talkub.runner import Runner

KINDS = ("software", "yubikey", "se")

_SECRET = re.compile(r"^AGE-SECRET-KEY-1[0-9A-Z]+$", re.MULTILINE)
_RECIPIENT = re.compile(r"(?:public key|recipient):\s*(age1[0-9a-z]+)", re.IGNORECASE)


def identities_dir(env: Mapping[str, str]) -> Path:
    base = env.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "talkub" / "identities"


def _first_secret(text: str, source: str) -> str:
    match = _SECRET.search(text)
    if match is None:
        raise NoIdentity(f"{source} does not contain an AGE-SECRET-KEY")
    return match.group(0)


def master_key_from_env(env: Mapping[str, str]) -> str | None:
    """Stage 1 of the resolution order: the master key itself (CI/automation)."""
    if value := env.get("SOPS_AGE_KEY", "").strip():
        return _first_secret(value, "SOPS_AGE_KEY")
    if path := env.get("SOPS_AGE_KEY_FILE"):
        try:
            return _first_secret(Path(path).expanduser().read_text(), "SOPS_AGE_KEY_FILE")
        except OSError as err:
            raise NoIdentity(f"cannot read SOPS_AGE_KEY_FILE {path}: {err.strerror}") from err
    return None


def identity_files(env: Mapping[str, str]) -> list[Path]:
    """Stages 2 and 3: TALKUB_IDENTITY, else every file in the identities dir."""
    if raw := env.get("TALKUB_IDENTITY"):
        files = [Path(part).expanduser() for part in raw.split(os.pathsep) if part]
        missing = [str(f) for f in files if not f.is_file()]
        if missing:
            raise NoIdentity(f"TALKUB_IDENTITY points to missing file(s): {', '.join(missing)}")
        return files
    directory = identities_dir(env)
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir() if p.is_file() and not p.name.startswith("."))


def recipient_of(path: Path) -> str | None:
    """Read the public key from the comment header of an identity file."""
    for line in path.read_text().splitlines():
        if line.lstrip().startswith("#") and (match := _RECIPIENT.search(line)):
            return match.group(1)
    return None


def create_identity(runner: Runner, kind: str, directory: Path, name: str | None = None) -> tuple[Path, str]:
    if kind not in KINDS:
        raise ValidationError(f"unknown identity kind {kind!r}", hint=f"use one of: {', '.join(KINDS)}")
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    path = directory / f"{name or kind}.txt"
    if path.exists():
        raise ValidationError(f"identity {path} already exists", hint="choose another --name")

    if kind == "software":
        runner.run(["age-keygen", "-o", str(path)])
    elif kind == "yubikey":
        # Interactive: the plugin asks for slot, PIN and touch policy on the terminal.
        if runner.call(["age-plugin-yubikey", "--generate"]) != 0:
            raise CommandFailed("age-plugin-yubikey --generate failed")
        atomic_write(path, runner.run(["age-plugin-yubikey", "--identity"]).stdout, mode=0o600)
    else:
        runner.run(
            [
                "age-plugin-se",
                "keygen",
                "--access-control=any-biometry",
                "-o",
                str(path),
            ]
        )

    os.chmod(path, 0o600)
    public = recipient_of(path)
    if public is None:
        raise ValidationError(f"could not read the public key from {path}")
    return path, public


def own_recipients(env: Mapping[str, str], user: str) -> list[Recipient]:
    """Recipients for the caller's own identities, named '<user>-<file stem>'."""
    files = identity_files(env)
    if not files:
        raise NoIdentity(
            "no age identity found",
            hint=f"run `talkub identity new` (stored in {identities_dir(env)}) or set TALKUB_IDENTITY",
        )
    safe_user = re.sub(r"[^A-Za-z0-9._-]", "-", user)
    result = []
    for path in files:
        key = recipient_of(path)
        if key is None:
            raise ValidationError(
                f"cannot determine the public key of {path}",
                hint="the file needs a '# public key: age1...' comment",
            )
        result.append(Recipient(f"{safe_user}-{path.stem}", key))
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_recipients.py tests/test_identity.py -v`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add src/talkub/recipients.py src/talkub/identity.py tests/test_recipients.py tests/test_identity.py
git commit -m "feat: recipients file format and identity resolution

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: age wrapper

**Files:**
- Create: `src/talkub/crypto/__init__.py` (empty), `src/talkub/crypto/age.py`
- Test: `tests/test_age.py`

**Interfaces:**
- Consumes: `Runner`, `CommandFailed`, `DecryptFailed`, `ValidationError`.
- Produces: `age.KeyPair(secret: str, public: str)`, `age.generate(runner) -> KeyPair`, `age.public_key(runner, secret) -> str`, `age.encrypt(runner, plaintext: bytes, recipients: Sequence[str]) -> bytes` (ASCII-armored), `age.decrypt(runner, path: Path, identities: Sequence[Path]) -> bytes`.

- [ ] **Step 1: Write the failing tests**

`tests/test_age.py`:

```python
import pytest

from helpers import FakeRunner, need
from talkub.crypto import age
from talkub.errors import DecryptFailed, ValidationError
from talkub.runner import Runner


@need("age", "age-keygen")
def test_roundtrip_with_multiple_recipients(tmp_path):
    runner = Runner()
    alice, bob = age.generate(runner), age.generate(runner)
    ciphertext = age.encrypt(runner, b"top secret\n", [alice.public, bob.public])
    assert ciphertext.startswith(b"-----BEGIN AGE ENCRYPTED FILE-----")
    encrypted = tmp_path / "x.age"
    encrypted.write_bytes(ciphertext)
    bob_identity = tmp_path / "bob.txt"
    bob_identity.write_text(bob.secret + "\n")
    assert age.decrypt(runner, encrypted, [bob_identity]) == b"top secret\n"


@need("age", "age-keygen")
def test_public_key_matches_generated():
    runner = Runner()
    pair = age.generate(runner)
    assert age.public_key(runner, pair.secret) == pair.public


@need("age", "age-keygen")
def test_decrypt_with_wrong_identity(tmp_path):
    runner = Runner()
    owner, stranger = age.generate(runner), age.generate(runner)
    encrypted = tmp_path / "x.age"
    encrypted.write_bytes(age.encrypt(runner, b"x", [owner.public]))
    identity = tmp_path / "stranger.txt"
    identity.write_text(stranger.secret + "\n")
    with pytest.raises(DecryptFailed) as exc:
        age.decrypt(runner, encrypted, [identity])
    assert "recipients.txt" in exc.value.hint


def test_decrypt_lets_plugin_prompts_reach_terminal(tmp_path):
    runner = FakeRunner()
    age.decrypt(runner, tmp_path / "m.age", [tmp_path / "yubikey.txt"])
    assert runner.calls[0].passthrough_stderr is True
    assert runner.calls[0].argv == [
        "age",
        "--decrypt",
        "-i",
        str(tmp_path / "yubikey.txt"),
        str(tmp_path / "m.age"),
    ]


def test_encrypt_requires_recipients():
    with pytest.raises(ValidationError):
        age.encrypt(FakeRunner(), b"x", [])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_age.py -v`
Expected: `ModuleNotFoundError: No module named 'talkub.crypto'`.

- [ ] **Step 3: Implement**

`src/talkub/crypto/__init__.py`: empty file.

`src/talkub/crypto/age.py`:

```python
"""Thin wrapper around the age and age-keygen binaries."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from talkub.errors import CommandFailed, DecryptFailed, ValidationError
from talkub.runner import Runner


@dataclass(frozen=True)
class KeyPair:
    secret: str
    public: str


def generate(runner: Runner) -> KeyPair:
    output = runner.run(["age-keygen"]).stdout.decode()
    secret = next(line for line in output.splitlines() if line.startswith("AGE-SECRET-KEY-"))
    return KeyPair(secret=secret, public=public_key(runner, secret))


def public_key(runner: Runner, secret: str) -> str:
    return runner.run(["age-keygen", "-y"], input=(secret + "\n").encode()).stdout.decode().strip()


def encrypt(runner: Runner, plaintext: bytes, recipients: Sequence[str]) -> bytes:
    if not recipients:
        raise ValidationError("cannot encrypt without recipients")
    argv = ["age", "--encrypt", "--armor"]
    for recipient in recipients:
        argv += ["-r", recipient]
    return runner.run(argv, input=plaintext).stdout


def decrypt(runner: Runner, path: Path, identities: Sequence[Path]) -> bytes:
    argv = ["age", "--decrypt"]
    for identity in identities:
        argv += ["-i", str(identity)]
    argv.append(str(path))
    try:
        # stderr stays on the terminal: plugins print "touch your YubiKey" / PIN prompts there.
        return runner.run(argv, passthrough_stderr=True).stdout
    except CommandFailed as err:
        raise DecryptFailed(
            f"could not decrypt {path.name}",
            hint="is your hardware key connected, and is your identity listed in keys/recipients.txt?",
        ) from err
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_age.py -v`
Expected: 5 passed (age's own error message appears on the terminal in the wrong-identity test; that is expected).

- [ ] **Step 5: Commit**

```bash
git add src/talkub/crypto tests/test_age.py
git commit -m "feat: age encrypt/decrypt wrapper

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Keyring (master key create/resolve/add/remove)

**Files:**
- Create: `src/talkub/keyring.py`
- Test: `tests/test_keyring.py`

**Interfaces:**
- Consumes: `Repo`, `Runner`, `age.*`, `identity.master_key_from_env`, `identity.identity_files`, `identity.identities_dir`, `recipients.*`, `atomic_write`.
- Produces: `Keyring(repo: Repo, runner: Runner, env: Mapping[str, str] | None = None)` with
  - `recipients() -> list[Recipient]`
  - `public_key() -> str`
  - `master_key() -> str` (cached)
  - `create(recipients: Sequence[Recipient]) -> str`
  - `add(recipient: Recipient) -> None`
  - `remove(name: str) -> None`
  - (`rotate(sops)` is added in Task 10)

- [ ] **Step 1: Write the failing tests**

`tests/test_keyring.py`:

```python
import pytest

from helpers import need
from talkub.crypto import age
from talkub.errors import DecryptFailed, NoIdentity, ValidationError
from talkub.identity import create_identity
from talkub.keyring import Keyring
from talkub.recipients import Recipient
from talkub.runner import Runner

pytestmark = need("age", "age-keygen")


def _other_identity(tmp_path, name):
    path, public = create_identity(Runner(), "software", tmp_path / f"ids-{name}", name)
    return path, public


def test_create_writes_key_files(empty_repo, software_identity):
    secret = Keyring(empty_repo, Runner()).create([Recipient("me", software_identity[1])])
    assert empty_repo.master_age.read_bytes().startswith(b"-----BEGIN AGE ENCRYPTED FILE-----")
    assert empty_repo.master_pub.read_text().strip() == age.public_key(Runner(), secret)
    assert "# name: me" in empty_repo.recipients_file.read_text()


def test_master_key_resolved_from_default_identities(keyed_repo):
    secret = Keyring(keyed_repo, Runner()).master_key()
    assert secret.startswith("AGE-SECRET-KEY-1")
    assert age.public_key(Runner(), secret) == keyed_repo.master_pub.read_text().strip()


def test_create_twice_rejected(keyed_repo, software_identity):
    with pytest.raises(ValidationError):
        Keyring(keyed_repo, Runner()).create([Recipient("me", software_identity[1])])


def test_env_master_key_wins_without_touching_master_age(keyed_repo):
    secret = Keyring(keyed_repo, Runner()).master_key()
    keyed_repo.master_age.unlink()
    assert Keyring(keyed_repo, Runner(), env={"SOPS_AGE_KEY": secret}).master_key() == secret


def test_talkub_identity_not_a_recipient(keyed_repo, tmp_path):
    stranger, _ = _other_identity(tmp_path, "stranger")
    with pytest.raises(DecryptFailed):
        Keyring(keyed_repo, Runner(), env={"TALKUB_IDENTITY": str(stranger)}).master_key()


def test_no_identity(keyed_repo, tmp_path):
    with pytest.raises(NoIdentity):
        Keyring(keyed_repo, Runner(), env={"XDG_CONFIG_HOME": str(tmp_path / "empty")}).master_key()


def test_add_lets_new_admin_decrypt(keyed_repo, tmp_path):
    bob, bob_public = _other_identity(tmp_path, "bob")
    Keyring(keyed_repo, Runner()).add(Recipient("bob", bob_public))
    as_bob = Keyring(keyed_repo, Runner(), env={"TALKUB_IDENTITY": str(bob)})
    assert as_bob.master_key() == Keyring(keyed_repo, Runner()).master_key()
    assert [r.name for r in as_bob.recipients()] == ["me", "bob"]


def test_add_duplicate_rejected(keyed_repo, software_identity):
    keyring = Keyring(keyed_repo, Runner())
    with pytest.raises(ValidationError):
        keyring.add(Recipient("me", "age1qk2vu7xxezxcfhyhasnx8c7z5j479xwevu5tqq8tc2fjqjzdegjqvepymq"))
    with pytest.raises(ValidationError):
        keyring.add(Recipient("other", software_identity[1]))


def test_remove_revokes_access(keyed_repo, tmp_path):
    bob, bob_public = _other_identity(tmp_path, "bob")
    keyring = Keyring(keyed_repo, Runner())
    keyring.add(Recipient("bob", bob_public))
    keyring.remove("bob")
    with pytest.raises(DecryptFailed):
        Keyring(keyed_repo, Runner(), env={"TALKUB_IDENTITY": str(bob)}).master_key()
    assert [r.name for r in keyring.recipients()] == ["me"]


def test_remove_last_recipient_refused(keyed_repo):
    with pytest.raises(ValidationError) as exc:
        Keyring(keyed_repo, Runner()).remove("me")
    assert "last recipient" in str(exc.value)


def test_remove_unknown(keyed_repo):
    with pytest.raises(ValidationError):
        Keyring(keyed_repo, Runner()).remove("nobody")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_keyring.py -v`
Expected: `ModuleNotFoundError: No module named 'talkub.keyring'`.

- [ ] **Step 3: Implement**

`src/talkub/keyring.py`:

```python
"""The cluster master key: keys/master.age, keys/master.pub and keys/recipients.txt.

All SOPS files are encrypted to the master key only. The master key itself is
encrypted to every admin recipient, so adding or removing an admin rewrites a
single small file instead of every SOPS file.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence

from talkub import identity
from talkub import recipients as recipients_file
from talkub.crypto import age
from talkub.errors import NoIdentity, ValidationError
from talkub.fsutil import atomic_write
from talkub.recipients import Recipient
from talkub.repo import Repo
from talkub.runner import Runner


class Keyring:
    def __init__(self, repo: Repo, runner: Runner, env: Mapping[str, str] | None = None) -> None:
        self.repo = repo
        self.runner = runner
        self.env = dict(os.environ if env is None else env)
        self._master: str | None = None

    def recipients(self) -> list[Recipient]:
        path = self.repo.recipients_file
        if not path.is_file():
            raise ValidationError(f"{self.repo.rel(path)} is missing")
        return recipients_file.parse(path.read_text())

    def public_key(self) -> str:
        return self.repo.master_pub.read_text().strip()

    def master_key(self) -> str:
        if self._master is None:
            self._master = self._resolve()
        return self._master

    def _resolve(self) -> str:
        if key := identity.master_key_from_env(self.env):
            return key
        files = identity.identity_files(self.env)
        if not files:
            raise NoIdentity(
                "no age identity found",
                hint=(
                    "create one with `talkub identity new` "
                    f"(stored in {identity.identities_dir(self.env)}) or set TALKUB_IDENTITY"
                ),
            )
        return age.decrypt(self.runner, self.repo.master_age, files).decode().strip()

    def create(self, recipients: Sequence[Recipient]) -> str:
        if self.repo.master_age.exists():
            raise ValidationError(f"{self.repo.rel(self.repo.master_age)} already exists")
        if not recipients:
            raise ValidationError("at least one recipient is required")
        pair = age.generate(self.runner)
        self._write(pair.secret, list(recipients))
        atomic_write(self.repo.master_pub, (pair.public + "\n").encode())
        self._master = pair.secret
        return pair.secret

    def add(self, recipient: Recipient) -> None:
        current = self.recipients()
        for existing in current:
            if existing.name == recipient.name:
                raise ValidationError(f"recipient '{recipient.name}' already exists")
            if existing.key == recipient.key:
                raise ValidationError(f"this key is already registered as '{existing.name}'")
        self._write(self.master_key(), [*current, recipient])

    def remove(self, name: str) -> None:
        current = self.recipients()
        remaining = [r for r in current if r.name != name]
        if len(remaining) == len(current):
            raise ValidationError(f"no recipient named '{name}'")
        if not remaining:
            raise ValidationError("refusing to remove the last recipient", hint="add another admin first")
        self._write(self.master_key(), remaining)

    def _write(self, secret: str, recipients: list[Recipient]) -> None:
        ciphertext = age.encrypt(self.runner, (secret + "\n").encode(), [r.key for r in recipients])
        # master.age first: if we stop in between, recipients.txt is merely stale
        # and re-running the same command fixes it.
        atomic_write(self.repo.master_age, ciphertext)
        atomic_write(self.repo.recipients_file, recipients_file.render(recipients).encode())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_keyring.py -v`
Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add src/talkub/keyring.py tests/test_keyring.py
git commit -m "feat: keyring for the cluster master key

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: SOPS wrapper, ephemeral decryption, Context

**Files:**
- Create: `src/talkub/crypto/sops.py`, `src/talkub/ephemeral.py`, `src/talkub/context.py`
- Test: `tests/test_sops.py`, `tests/test_ephemeral.py`

**Interfaces:**
- Consumes: `Runner`, `Keyring`, `Repo`, errors.
- Produces:
  - `sops.render_sops_config(public_key: str) -> str`
  - `sops.Sops(runner: Runner, master_key: Callable[[], str])` with `decrypt(path: Path) -> bytes`, `encrypt(plaintext: bytes, *, filename: str, public_key: str) -> bytes`, `edit(path: Path, cwd: Path) -> int`
  - `ephemeral.private_tempdir() -> ContextManager[Path]`
  - `ephemeral.decrypted(sops, files: Mapping[str, Path]) -> ContextManager[dict[str, Path]]` (`sops` is anything with `decrypt(path) -> bytes`)
  - `context.Context(repo, runner, keyring, sops)` with `Context.open(repo: Repo, runner: Runner | None = None, env: Mapping[str, str] | None = None) -> Context`

- [ ] **Step 1: Write the failing tests**

`tests/test_sops.py`:

```python
import pytest

from helpers import need
from talkub import yamlio
from talkub.context import Context
from talkub.crypto.sops import Sops, render_sops_config
from talkub.errors import DecryptFailed
from talkub.runner import Runner

pytestmark = need("age", "age-keygen", "sops")


def test_encrypt_decrypt_roundtrip(keyed_repo, tmp_path):
    ctx = Context.open(keyed_repo)
    ciphertext = ctx.sops.encrypt(
        b"a: 1\nb: secret\n",
        filename="x.sops.yaml",
        public_key=ctx.keyring.public_key(),
    )
    assert "sops" in yamlio.load(ciphertext.decode())
    assert b"secret" not in ciphertext
    path = tmp_path / "x.sops.yaml"
    path.write_bytes(ciphertext)
    assert yamlio.load(ctx.sops.decrypt(path).decode()) == {"a": 1, "b": "secret"}


def test_encrypt_needs_no_private_key(keyed_repo):
    def no_key():
        raise AssertionError("encrypt must not resolve the master key")

    public = Context.open(keyed_repo).keyring.public_key()
    assert Sops(Runner(), no_key).encrypt(b"a: 1\n", filename="x.sops.yaml", public_key=public)


def test_decrypt_with_wrong_key(keyed_repo, tmp_path):
    ctx = Context.open(keyed_repo)
    path = tmp_path / "x.sops.yaml"
    path.write_bytes(ctx.sops.encrypt(b"a: 1\n", filename=path.name, public_key=ctx.keyring.public_key()))
    wrong = "AGE-SECRET-KEY-133FP6CADJCE6M9H6AUXY3NLXNX73NSSLQS0CHGVLPSVZKK87V80SPY0R56"
    with pytest.raises(DecryptFailed):
        Sops(Runner(), lambda: wrong).decrypt(path)


def test_render_sops_config():
    text = render_sops_config("age1abc")
    assert yamlio.load(text) == {"creation_rules": [{"path_regex": r"\.sops\.yaml$", "age": "age1abc"}]}
```

`tests/test_ephemeral.py`:

```python
import os
import signal
from pathlib import Path

import pytest

from talkub.ephemeral import decrypted, private_tempdir


class FakeSops:
    def decrypt(self, path: Path) -> bytes:
        return f"plain:{path.name}".encode()


def test_decrypted_files_are_private_and_removed(tmp_path):
    with decrypted(FakeSops(), {"talosconfig": tmp_path / "t.sops.yaml"}) as paths:
        target = paths["talosconfig"]
        assert target.read_bytes() == b"plain:t.sops.yaml"
        assert target.stat().st_mode & 0o777 == 0o600
        assert target.parent.stat().st_mode & 0o777 == 0o700
    assert not target.exists()
    assert not target.parent.exists()


def test_removed_when_body_raises(tmp_path):
    with pytest.raises(RuntimeError):
        with decrypted(FakeSops(), {"kubeconfig": tmp_path / "k.sops.yaml"}) as paths:
            target = paths["kubeconfig"]
            raise RuntimeError("boom")
    assert not target.exists()


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_removed_on_termination_signal(tmp_path, signum):
    previous = signal.getsignal(signum)
    with pytest.raises(SystemExit) as exc:
        with decrypted(FakeSops(), {"talosconfig": tmp_path / "t.sops.yaml"}) as paths:
            target = paths["talosconfig"]
            os.kill(os.getpid(), signum)
    assert exc.value.code == 128 + signum
    assert not target.exists()
    assert signal.getsignal(signum) == previous


def test_private_tempdir_mode():
    with private_tempdir() as path:
        assert path.stat().st_mode & 0o777 == 0o700
    assert not path.exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_sops.py tests/test_ephemeral.py -v`
Expected: `ModuleNotFoundError` for `talkub.context` / `talkub.ephemeral`.

- [ ] **Step 3: Implement**

`src/talkub/crypto/sops.py`:

```python
"""Thin wrapper around the sops binary.

The master key is handed to sops via SOPS_AGE_KEY in the child environment only.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from talkub.errors import CommandFailed, DecryptFailed
from talkub.runner import Runner


def render_sops_config(public_key: str) -> str:
    return (
        "# Managed by talkub. Every *.sops.yaml is encrypted to the cluster master key.\n"
        "creation_rules:\n"
        "  - path_regex: \\.sops\\.yaml$\n"
        f"    age: {public_key}\n"
    )


class Sops:
    def __init__(self, runner: Runner, master_key: Callable[[], str]) -> None:
        self.runner = runner
        self._master_key = master_key

    def _key_env(self) -> dict[str, str]:
        return {"SOPS_AGE_KEY": self._master_key()}

    def decrypt(self, path: Path) -> bytes:
        try:
            return self.runner.run(["sops", "decrypt", str(path)], env=self._key_env()).stdout
        except CommandFailed as err:
            raise DecryptFailed(f"sops could not decrypt {path}", hint=err.hint) from err

    def encrypt(self, plaintext: bytes, *, filename: str, public_key: str) -> bytes:
        argv = [
            "sops",
            "encrypt",
            "--age",
            public_key,
            "--input-type",
            "yaml",
            "--output-type",
            "yaml",
            "--filename-override",
            filename,
            "/dev/stdin",
        ]
        return self.runner.run(argv, input=plaintext).stdout

    def edit(self, path: Path, cwd: Path) -> int:
        return self.runner.call(["sops", "edit", str(path)], env=self._key_env(), cwd=cwd)
```

`src/talkub/ephemeral.py`:

```python
"""Short-lived plaintext: private temp dirs that are always removed."""

from __future__ import annotations

import os
import shutil
import signal
import tempfile
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

# SIGHUP covers a closed terminal window.
_TERMINATION_SIGNALS = (signal.SIGTERM, signal.SIGHUP)


class Decryptor(Protocol):
    def decrypt(self, path: Path) -> bytes: ...


@contextmanager
def private_tempdir() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="talkub-"))  # mkdtemp creates it with mode 0700
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@contextmanager
def _termination_as_exit() -> Iterator[None]:
    """Turn SIGTERM/SIGHUP into SystemExit so `finally` blocks run and clean up."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def handler(signum, frame):
        raise SystemExit(128 + signum)

    previous = {sig: signal.signal(sig, handler) for sig in _TERMINATION_SIGNALS}
    try:
        yield
    finally:
        for sig, old in previous.items():
            signal.signal(sig, old)


@contextmanager
def decrypted(sops: Decryptor, files: Mapping[str, Path]) -> Iterator[dict[str, Path]]:
    """Decrypt `files` (name -> encrypted path) into a private temp dir for the block's duration."""
    with _termination_as_exit(), private_tempdir() as directory:
        paths: dict[str, Path] = {}
        for name, source in files.items():
            target = directory / name
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(sops.decrypt(source))
            paths[name] = target
        yield paths
```

`src/talkub/context.py`:

```python
"""Everything a command needs to work on one cluster repo."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from talkub.crypto.sops import Sops
from talkub.keyring import Keyring
from talkub.repo import Repo
from talkub.runner import Runner


@dataclass
class Context:
    repo: Repo
    runner: Runner
    keyring: Keyring
    sops: Sops

    @classmethod
    def open(
        cls,
        repo: Repo,
        runner: Runner | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Context:
        runner = runner or Runner()
        keyring = Keyring(repo, runner, env)
        return cls(
            repo=repo,
            runner=runner,
            keyring=keyring,
            sops=Sops(runner, keyring.master_key),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_sops.py tests/test_ephemeral.py -v`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add src/talkub/crypto/sops.py src/talkub/ephemeral.py src/talkub/context.py tests/test_sops.py tests/test_ephemeral.py
git commit -m "feat: sops wrapper and signal-safe ephemeral decryption

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Credential access for talosctl, kubectl and shell

**Files:**
- Create: `src/talkub/access.py`
- Test: `tests/test_access.py`

**Interfaces:**
- Consumes: `Context`, `ephemeral.decrypted`, `ValidationError`.
- Produces: `access.talosctl(ctx: Context, args: Sequence[str]) -> int`, `access.kubectl(ctx, args) -> int`, `access.shell(ctx, program: str | None = None) -> int`.

- [ ] **Step 1: Write the failing tests**

`tests/test_access.py` (uses fake `talosctl`/`kubectl`/shell scripts on PATH that print what they received):

```python
import os

import pytest

from helpers import need
from talkub import access
from talkub.context import Context
from talkub.errors import ValidationError

pytestmark = need("age", "age-keygen", "sops")

REPORT = """printf 'args:%s\\n' "$*"
printf 'key:%s\\n' "${SOPS_AGE_KEY:-absent}"
printf 'ident:%s\\n' "${TALKUB_IDENTITY:-absent}"
printf 'session:%s\\n' "${TALKUB_SESSION:-none}"
[ -n "$TALOSCONFIG" ] && [ -f "$TALOSCONFIG" ] && cat "$TALOSCONFIG"
[ -n "$KUBECONFIG" ] && [ -f "$KUBECONFIG" ] && cat "$KUBECONFIG"
printf 'tc:%s\\n' "$TALOSCONFIG"
exit 5
"""


def test_talosctl_gets_decrypted_config_and_verbatim_args(
    repo_with_configs, software_identity, fake_bin, capfd, monkeypatch
):
    fake_bin("talosctl", REPORT)
    # Used by talkub to decrypt, but must not be visible to talosctl.
    monkeypatch.setenv("TALKUB_IDENTITY", str(software_identity[0]))
    code = access.talosctl(Context.open(repo_with_configs), ["get", "members", "--", "-x"])
    out = capfd.readouterr().out
    assert code == 5
    assert "args:get members -- -x" in out
    assert "talos: marker" in out
    assert "key:absent" in out
    assert "ident:absent" in out
    talosconfig = out.split("tc:")[1].strip()
    assert talosconfig and not os.path.exists(talosconfig)


def test_kubectl_uses_kubeconfig(repo_with_configs, fake_bin, capfd):
    fake_bin("kubectl", REPORT)
    assert access.kubectl(Context.open(repo_with_configs), ["get", "pods"]) == 5
    assert "kube: marker" in capfd.readouterr().out


def test_kubectl_without_kubeconfig_explains(keyed_repo):
    with pytest.raises(ValidationError) as exc:
        access.kubectl(Context.open(keyed_repo), ["get", "pods"])
    assert "bootstrap" in exc.value.hint


def test_shell_sets_session_and_both_configs(repo_with_configs, fake_bin, capfd):
    program = fake_bin("fakeshell", REPORT)
    assert access.shell(Context.open(repo_with_configs), str(program)) == 5
    captured = capfd.readouterr()
    assert "session:test" in captured.out
    assert "talos: marker" in captured.out and "kube: marker" in captured.out
    assert "credentials removed" in captured.err


def test_shell_without_kubeconfig_does_not_leak_parent_kubeconfig(
    repo_with_configs, fake_bin, capfd, monkeypatch, tmp_path
):
    repo_with_configs.kubeconfig.unlink()
    other = tmp_path / "other-cluster.yaml"
    other.write_text("other: cluster\n")
    monkeypatch.setenv("KUBECONFIG", str(other))
    program = fake_bin("fakeshell", REPORT)
    access.shell(Context.open(repo_with_configs), str(program))
    assert "other: cluster" not in capfd.readouterr().out


def test_nested_shell_refused(repo_with_configs, monkeypatch):
    monkeypatch.setenv("TALKUB_SESSION", "test")
    with pytest.raises(ValidationError):
        access.shell(Context.open(repo_with_configs), "/bin/sh")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_access.py -v`
Expected: `ImportError: cannot import name 'access'`.

- [ ] **Step 3: Implement**

`src/talkub/access.py`:

```python
"""Run talosctl, kubectl or a shell with credentials decrypted only for the duration."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from pathlib import Path

from talkub.context import Context
from talkub.ephemeral import decrypted
from talkub.errors import ValidationError

TALOSCONFIG_HINT = "it is created by `talkub init`"
KUBECONFIG_HINT = "it is created by `talkub bootstrap`"


def _existing(ctx: Context, path: Path, hint: str) -> Path:
    if not path.is_file():
        raise ValidationError(f"{ctx.repo.rel(path)} does not exist", hint=hint)
    return path


def talosctl(ctx: Context, args: Sequence[str]) -> int:
    source = _existing(ctx, ctx.repo.talosconfig, TALOSCONFIG_HINT)
    with decrypted(ctx.sops, {"talosconfig": source}) as paths:
        return ctx.runner.call(["talosctl", *args], env={"TALOSCONFIG": str(paths["talosconfig"])})


def kubectl(ctx: Context, args: Sequence[str]) -> int:
    source = _existing(ctx, ctx.repo.kubeconfig, KUBECONFIG_HINT)
    with decrypted(ctx.sops, {"kubeconfig": source}) as paths:
        return ctx.runner.call(["kubectl", *args], env={"KUBECONFIG": str(paths["kubeconfig"])})


def shell(ctx: Context, program: str | None = None) -> int:
    if active := os.environ.get("TALKUB_SESSION"):
        raise ValidationError(f"already inside a talkub shell for '{active}'", hint="exit it first")
    name = ctx.repo.load_settings().name
    files = {"talosconfig": _existing(ctx, ctx.repo.talosconfig, TALOSCONFIG_HINT)}
    if ctx.repo.kubeconfig.is_file():
        files["kubeconfig"] = ctx.repo.kubeconfig
    with decrypted(ctx.sops, files) as paths:
        workdir = paths["talosconfig"].parent
        env = {
            "TALOSCONFIG": str(paths["talosconfig"]),
            # Without a kubeconfig, point at a missing file so kubectl cannot silently
            # talk to whatever cluster the parent KUBECONFIG selects.
            "KUBECONFIG": str(paths.get("kubeconfig", workdir / "no-kubeconfig-yet")),
            "TALKUB_SESSION": name,
        }
        print(
            f"talkub: shell for '{name}' - credentials are removed on exit",
            file=sys.stderr,
        )
        code = ctx.runner.call([program or os.environ.get("SHELL") or "/bin/sh"], env=env)
    print("talkub: credentials removed", file=sys.stderr)
    return code
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_access.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/talkub/access.py tests/test_access.py
git commit -m "feat: ephemeral credentials for talosctl, kubectl and shell

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Talos generation helpers and chart lookup

**Files:**
- Create: `src/talkub/talos/__init__.py` (empty), `src/talkub/talos/gen.py`, `src/talkub/charts.py`
- Test: `tests/test_talos_gen.py`, `tests/test_charts.py`

**Interfaces:**
- Consumes: `Runner`, `yamlio`, `ValidationError`, `ChartSpec`.
- Produces:
  - `gen.Defaults(talos_version: str, kubernetes_version: str, install_image: str)`
  - `gen.gen_secrets(runner, workdir: Path) -> Path`
  - `gen.parse_defaults(text: str) -> Defaults`
  - `gen.detect_defaults(runner, name: str, endpoint: str, secrets: Path) -> Defaults`
  - `gen.gen_talosconfig(runner, name, endpoint, secrets: Path, workdir: Path, *, endpoints: Sequence[str], node: str) -> Path`
  - `charts.DEFAULT_CHARTS: dict[str, tuple[str, str]]` (key → (repo URL, chart name))
  - `charts.latest_version(runner, repo: str, chart: str) -> str`
  - `charts.resolve_defaults(runner) -> dict[str, ChartSpec]`

- [ ] **Step 1: Write the failing tests**

`tests/test_talos_gen.py`:

```python
import re

import pytest

from helpers import need
from talkub import yamlio
from talkub.errors import ValidationError
from talkub.runner import Runner
from talkub.talos import gen

MULTIDOC = """version: v1alpha1
machine:
    type: controlplane
---
apiVersion: v1alpha1
kind: UnattendedInstallConfig
installer:
    image: factory.talos.dev/metal-installer/376567:v1.14.0
---
apiVersion: v1alpha1
kind: KubeletConfig
image: ghcr.io/siderolabs/kubelet:v1.37.0
"""

LEGACY = """version: v1alpha1
machine:
    install:
        image: ghcr.io/siderolabs/installer:v1.9.0
    kubelet:
        image: ghcr.io/siderolabs/kubelet:v1.32.0
"""


def test_parse_defaults_multidoc():
    assert gen.parse_defaults(MULTIDOC) == gen.Defaults(
        talos_version="v1.14.0",
        kubernetes_version="1.37.0",
        install_image="factory.talos.dev/metal-installer/376567",
    )


def test_parse_defaults_legacy_v1alpha1():
    assert gen.parse_defaults(LEGACY) == gen.Defaults("v1.9.0", "1.32.0", "ghcr.io/siderolabs/installer")


def test_parse_defaults_unknown_layout():
    with pytest.raises(ValidationError):
        gen.parse_defaults("version: v1alpha1\n")


@need("talosctl")
def test_detect_defaults_and_talosconfig(tmp_path):
    runner = Runner()
    secrets = gen.gen_secrets(runner, tmp_path)
    defaults = gen.detect_defaults(runner, "demo", "https://10.0.0.10:6443", secrets)
    assert re.fullmatch(r"v\d+\.\d+\.\d+", defaults.talos_version)
    assert re.fullmatch(r"\d+\.\d+\.\d+", defaults.kubernetes_version)
    config = gen.gen_talosconfig(
        runner,
        "demo",
        "https://10.0.0.10:6443",
        secrets,
        tmp_path,
        endpoints=["10.0.0.10"],
        node="10.0.0.10",
    )
    context = yamlio.load(config.read_text())["contexts"]["demo"]
    assert list(context["endpoints"]) == ["10.0.0.10"]
    assert list(context["nodes"]) == ["10.0.0.10"]
```

`tests/test_charts.py`:

```python
from helpers import FakeRunner
from talkub import charts

CHART_YAML = b"apiVersion: v2\nname: argo-cd\nversion: 10.9.2\nappVersion: v3.5.3\n"


def test_latest_version_http_repo():
    runner = FakeRunner(outputs={"helm": CHART_YAML})
    assert charts.latest_version(runner, "https://argoproj.github.io/argo-helm", "argo-cd") == "10.9.2"
    assert runner.calls[0].argv == [
        "helm",
        "show",
        "chart",
        "argo-cd",
        "--repo",
        "https://argoproj.github.io/argo-helm",
    ]


def test_latest_version_oci_repo():
    runner = FakeRunner(outputs={"helm": CHART_YAML})
    charts.latest_version(runner, "oci://ghcr.io/example/charts", "thing")
    assert runner.calls[0].argv == [
        "helm",
        "show",
        "chart",
        "oci://ghcr.io/example/charts/thing",
    ]


def test_resolve_defaults_covers_both_charts():
    specs = charts.resolve_defaults(FakeRunner(outputs={"helm": CHART_YAML}))
    assert set(specs) == {"argocd", "sealed-secrets"}
    assert specs["sealed-secrets"].repo == "https://bitnami.github.io/sealed-secrets"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_talos_gen.py tests/test_charts.py -v`
Expected: `ModuleNotFoundError` for `talkub.talos` / `talkub.charts`.

- [ ] **Step 3: Implement**

`src/talkub/talos/__init__.py`: empty file.

`src/talkub/talos/gen.py`:

```python
"""talosctl gen helpers used by `init` (and later by rendering in phase 2)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from talkub import yamlio
from talkub.errors import ValidationError
from talkub.runner import Runner


@dataclass(frozen=True)
class Defaults:
    talos_version: str
    kubernetes_version: str
    install_image: str


def gen_secrets(runner: Runner, workdir: Path) -> Path:
    path = workdir / "secrets.yaml"
    runner.run(["talosctl", "gen", "secrets", "-o", str(path)])
    return path


def _image_tag(reference: str) -> tuple[str, str]:
    image, _, tag = reference.rpartition(":")
    return image, tag


def parse_defaults(text: str) -> Defaults:
    """Read installer image and kubelet version from a generated controlplane config.

    Talos >= 1.12 emits separate documents (UnattendedInstallConfig, KubeletConfig);
    older versions keep both inside the v1alpha1 document.
    """
    docs = [doc for doc in yamlio.load_all(text) if isinstance(doc, Mapping)]
    installer = kubelet = None
    for doc in docs:
        kind = doc.get("kind")
        if kind == "UnattendedInstallConfig":
            installer = (doc.get("installer") or {}).get("image")
        elif kind == "KubeletConfig":
            kubelet = doc.get("image")
        elif doc.get("version") == "v1alpha1" and isinstance(doc.get("machine"), Mapping):
            machine = doc["machine"]
            installer = installer or (machine.get("install") or {}).get("image")
            kubelet = kubelet or (machine.get("kubelet") or {}).get("image")
    if not installer or not kubelet:
        raise ValidationError(
            "could not detect installer image and Kubernetes version from talosctl output",
            hint="check `talosctl version --client`; this talosctl may be unsupported",
        )
    image, talos_version = _image_tag(installer)
    _, kubelet_tag = _image_tag(kubelet)
    return Defaults(
        talos_version=talos_version,
        kubernetes_version=kubelet_tag.lstrip("v"),
        install_image=image,
    )


def detect_defaults(runner: Runner, name: str, endpoint: str, secrets: Path) -> Defaults:
    argv = [
        "talosctl",
        "gen",
        "config",
        name,
        endpoint,
        "--with-secrets",
        str(secrets),
        "-t",
        "controlplane",
        "-o",
        "-",
        "--with-docs=false",
        "--with-examples=false",
    ]
    return parse_defaults(runner.run(argv).stdout.decode())


def gen_talosconfig(
    runner: Runner,
    name: str,
    endpoint: str,
    secrets: Path,
    workdir: Path,
    *,
    endpoints: Sequence[str],
    node: str,
) -> Path:
    path = workdir / "talosconfig"
    runner.run(
        [
            "talosctl",
            "gen",
            "config",
            name,
            endpoint,
            "--with-secrets",
            str(secrets),
            "-t",
            "talosconfig",
            "-o",
            str(path),
        ]
    )
    runner.run(["talosctl", "--talosconfig", str(path), "config", "endpoint", *endpoints])
    runner.run(["talosctl", "--talosconfig", str(path), "config", "node", node])
    return path
```

`src/talkub/charts.py`:

```python
"""Helm charts installed by bootstrap and managed by ArgoCD afterwards."""

from __future__ import annotations

from talkub import yamlio
from talkub.repo import ChartSpec
from talkub.runner import Runner

DEFAULT_CHARTS: dict[str, tuple[str, str]] = {
    "argocd": ("https://argoproj.github.io/argo-helm", "argo-cd"),
    "sealed-secrets": ("https://bitnami.github.io/sealed-secrets", "sealed-secrets"),
}


def latest_version(runner: Runner, repo: str, chart: str) -> str:
    if repo.startswith("oci://"):
        argv = ["helm", "show", "chart", f"{repo.rstrip('/')}/{chart}"]
    else:
        argv = ["helm", "show", "chart", chart, "--repo", repo]
    return str(yamlio.load(runner.run(argv).stdout.decode())["version"])


def resolve_defaults(runner: Runner) -> dict[str, ChartSpec]:
    return {
        key: ChartSpec(repo=repo, chart=chart, version=latest_version(runner, repo, chart))
        for key, (repo, chart) in DEFAULT_CHARTS.items()
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_talos_gen.py tests/test_charts.py -v`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add src/talkub/talos src/talkub/charts.py tests/test_talos_gen.py tests/test_charts.py
git commit -m "feat: talosctl generation helpers and helm chart lookup

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: Scaffold and `init_cluster`

**Files:**
- Create: `src/talkub/scaffold.py`
- Create templates under `src/talkub/templates/` (listed in Step 3)
- Test: `tests/test_scaffold.py`

**Interfaces:**
- Consumes: `Repo`, `Settings`, `NodeSpec`, `Keyring`, `Sops`, `render_sops_config`, `private_tempdir`, `gen.*`, `charts.resolve_defaults`, `atomic_write`, `Recipient`.
- Produces:
  - `scaffold.InitRequest(target: Path, name: str, endpoint: str, git_url: str, nodes: dict[str, NodeSpec], recipients: list[Recipient])`
  - `scaffold.init_cluster(request: InitRequest, runner: Runner) -> Repo`
  - `scaffold.render_node_patch(node: str) -> str` (reused by `node add` in phase 2)

- [ ] **Step 1: Write the failing tests**

`tests/test_scaffold.py`:

```python
import subprocess

import pytest

from helpers import need
from talkub import yamlio
from talkub.context import Context
from talkub.ephemeral import decrypted
from talkub.errors import CommandFailed, ValidationError
from talkub.recipients import Recipient
from talkub.repo import NodeSpec
from talkub.runner import Runner
from talkub.scaffold import InitRequest, init_cluster

pytestmark = need("age", "age-keygen", "sops", "talosctl")

HELM = "printf 'apiVersion: v2\\nname: x\\nversion: 1.2.3\\n'\n"


def _request(target, public):
    return InitRequest(
        target=target,
        name="demo",
        endpoint="https://10.0.0.10:6443",
        git_url="https://git.example.com/demo.git",
        nodes={"cp-01": NodeSpec(ip="10.0.0.10")},
        recipients=[Recipient("me", public)],
    )


def test_init_creates_complete_repo(tmp_path, software_identity, fake_bin):
    fake_bin("helm", HELM)
    repo = init_cluster(_request(tmp_path / "demo", software_identity[1]), Runner())

    settings = repo.load_settings()
    assert settings.name == "demo"
    assert settings.nodes == {"cp-01": NodeSpec(ip="10.0.0.10")}
    assert settings.charts["argocd"].version == "1.2.3"
    assert settings.talos_version.startswith("v")

    for rel in (
        ".gitignore",
        ".sops.yaml",
        "README.md",
        "keys/recipients.txt",
        "keys/master.age",
        "keys/master.pub",
        "talos/secrets.sops.yaml",
        "talos/talosconfig.sops.yaml",
        "talos/patches/controlplane.yaml",
        "talos/nodes/cp-01.yaml",
        "gitops/root.yaml",
        "gitops/apps/argocd.yaml",
        "gitops/apps/sealed-secrets.yaml",
        "gitops/values/argocd.yaml",
        "gitops/values/sealed-secrets.yaml",
        "gitops/manifests/.gitkeep",
    ):
        assert (repo.root / rel).is_file(), rel

    root_app = yamlio.load((repo.root / "gitops/root.yaml").read_text())
    assert root_app["spec"]["source"]["repoURL"] == "https://git.example.com/demo.git"
    argocd_app = yamlio.load((repo.root / "gitops/apps/argocd.yaml").read_text())
    assert argocd_app["spec"]["sources"][0]["targetRevision"] == "1.2.3"
    assert argocd_app["spec"]["sources"][0]["helm"]["valueFiles"] == ["$values/gitops/values/argocd.yaml"]

    ctx = Context.open(repo)
    talosconfig = yamlio.load(ctx.sops.decrypt(repo.talosconfig).decode())
    assert list(talosconfig["contexts"]["demo"]["endpoints"]) == ["10.0.0.10"]


def test_generated_patches_validate_with_talosctl(tmp_path, software_identity, fake_bin):
    fake_bin("helm", HELM)
    repo = init_cluster(_request(tmp_path / "demo", software_identity[1]), Runner())
    ctx = Context.open(repo)
    with decrypted(ctx.sops, {"secrets.yaml": repo.talos_secrets}) as paths:
        out = paths["secrets.yaml"].parent / "cp.yaml"
        subprocess.run(
            [
                "talosctl",
                "gen",
                "config",
                "demo",
                "https://10.0.0.10:6443",
                "--with-secrets",
                str(paths["secrets.yaml"]),
                "-t",
                "controlplane",
                "-o",
                str(out),
                "--with-docs=false",
                "--with-examples=false",
                "--config-patch-control-plane",
                f"@{repo.root / 'talos/patches/controlplane.yaml'}",
                "--config-patch",
                f"@{repo.root / 'talos/nodes/cp-01.yaml'}",
            ],
            check=True,
            capture_output=True,
        )
        result = subprocess.run(
            ["talosctl", "validate", "--config", str(out), "--mode", "metal"],
            capture_output=True,
            text=True,
        )
        rendered = out.read_text()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "hostname: cp-01" in rendered
    assert "NoSchedule" not in rendered


def test_non_empty_target_refused(tmp_path, software_identity):
    target = tmp_path / "demo"
    target.mkdir()
    (target / "something").write_text("x")
    with pytest.raises(ValidationError):
        init_cluster(_request(target, software_identity[1]), Runner())


def test_existing_git_dir_is_allowed(tmp_path, software_identity, fake_bin):
    fake_bin("helm", HELM)
    target = tmp_path / "demo"
    (target / ".git").mkdir(parents=True)
    init_cluster(_request(target, software_identity[1]), Runner())
    assert (target / ".talkub.yaml").is_file()


def test_failed_helm_lookup_writes_nothing(tmp_path, software_identity, fake_bin):
    fake_bin("helm", "echo 'network unreachable' >&2; exit 1\n")
    target = tmp_path / "demo"
    with pytest.raises(CommandFailed):
        init_cluster(_request(target, software_identity[1]), Runner())
    assert not target.exists()


def test_failure_after_writing_cleans_up_and_can_be_retried(tmp_path, software_identity, fake_bin):
    fake_bin("helm", HELM)
    # Fails after keys/ has been written, so cleanup is really exercised.
    broken_talosctl = fake_bin("talosctl", "echo 'boom' >&2; exit 1\n")
    target = tmp_path / "demo"
    (target / ".git").mkdir(parents=True)
    with pytest.raises(CommandFailed):
        init_cluster(_request(target, software_identity[1]), Runner())
    assert sorted(p.name for p in target.iterdir()) == [".git"]

    broken_talosctl.unlink()
    init_cluster(_request(target, software_identity[1]), Runner())
    assert (target / "keys/master.age").is_file()


def test_needs_a_controlplane_node(tmp_path, software_identity):
    request = _request(tmp_path / "demo", software_identity[1])
    request.nodes["cp-01"] = NodeSpec(ip="10.0.0.10", role="worker")
    with pytest.raises(ValidationError):
        init_cluster(request, Runner())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_scaffold.py -v`
Expected: `ModuleNotFoundError: No module named 'talkub.scaffold'`.

- [ ] **Step 3: Create the templates**

`src/talkub/templates/node.yaml.tmpl`:

```yaml
# Talos patch for node $node, applied after talos/patches/*.yaml.
# Add node-specific settings here (install disk, network, ...).
apiVersion: v1alpha1
kind: HostnameConfig
auto: "off"
hostname: $node
```

`src/talkub/templates/repo/dot-gitignore`:

```
# Plaintext must never be committed. talkub only decrypts into private temp dirs.
*.dec
*.dec.*
.talkub-tmp/
talos/secrets.yaml
talos/talosconfig
kubernetes/kubeconfig
```

`src/talkub/templates/repo/README.md.tmpl`:

```markdown
# $name

Talos cluster repo managed with [talkub](https://github.com/).

| Path | Content |
|---|---|
| `keys/` | cluster master key (`master.age`, encrypted to `recipients.txt`) |
| `talos/` | Talos secrets and talosconfig (SOPS), patches, per-node patches |
| `kubernetes/` | kubeconfig (SOPS), created by `talkub bootstrap` |
| `bootstrap/` | sealed-secrets key backup (SOPS) and public cert |
| `gitops/` | everything ArgoCD reads (`root.yaml` is the app of apps) |

Common commands:

    talkub t get members        # talosctl with the decrypted talosconfig
    talkub k get pods -A        # kubectl with the decrypted kubeconfig
    talkub shell                # subshell with TALOSCONFIG/KUBECONFIG set
    talkub admin add <age1...> --name <admin>
    talkub check                # run before every commit
```

`src/talkub/templates/repo/talos/patches/controlplane.yaml`:

```yaml
# Allow regular workloads on control plane nodes (single-node cluster):
# removes the default node-role.kubernetes.io/control-plane:NoSchedule taint.
apiVersion: v1alpha1
kind: KubeNodeConfig
taints:
  $patch: delete
```

`src/talkub/templates/repo/gitops/root.yaml.tmpl`:

```yaml
# App of apps: every Application in gitops/apps/ is managed from here.
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: root
  namespace: argocd
spec:
  project: default
  source:
    repoURL: $git_url
    targetRevision: HEAD
    path: gitops/apps
  destination:
    server: https://kubernetes.default.svc
    namespace: argocd
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
```

`src/talkub/templates/repo/gitops/apps/argocd.yaml.tmpl`:

```yaml
# Argo CD manages itself after `talkub bootstrap` installed it once.
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: argocd
  namespace: argocd
spec:
  project: default
  sources:
    - repoURL: $argocd_repo
      chart: $argocd_chart
      targetRevision: $argocd_version
      helm:
        valueFiles:
          - $$values/gitops/values/argocd.yaml
    - repoURL: $git_url
      targetRevision: HEAD
      ref: values
  destination:
    server: https://kubernetes.default.svc
    namespace: argocd
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - ServerSideApply=true
```

`src/talkub/templates/repo/gitops/apps/sealed-secrets.yaml.tmpl`:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: sealed-secrets
  namespace: argocd
spec:
  project: default
  sources:
    - repoURL: $sealed_secrets_repo
      chart: $sealed_secrets_chart
      targetRevision: $sealed_secrets_version
      helm:
        valueFiles:
          - $$values/gitops/values/sealed-secrets.yaml
    - repoURL: $git_url
      targetRevision: HEAD
      ref: values
  destination:
    server: https://kubernetes.default.svc
    namespace: kube-system
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
```

`src/talkub/templates/repo/gitops/values/argocd.yaml`:

```yaml
# Helm values for Argo CD (chart argo-cd). Used by `talkub bootstrap` and by Argo CD itself.
dex:
  enabled: false
```

`src/talkub/templates/repo/gitops/values/sealed-secrets.yaml`:

```yaml
# Helm values for sealed-secrets. Used by `talkub bootstrap` and by Argo CD.
# The name matches kubeseal's default --controller-name.
fullnameOverride: sealed-secrets-controller
```

`src/talkub/templates/repo/gitops/manifests/dot-gitkeep`: empty file.

- [ ] **Step 4: Implement `scaffold.py`**

`src/talkub/scaffold.py`:

```python
"""`talkub init`: create a new cluster repo from the packaged templates."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from string import Template

from talkub import charts
from talkub.crypto.sops import Sops, render_sops_config
from talkub.ephemeral import private_tempdir
from talkub.errors import ValidationError
from talkub.fsutil import atomic_write
from talkub.keyring import Keyring
from talkub.recipients import Recipient
from talkub.repo import NodeSpec, Repo, Settings
from talkub.runner import Runner
from talkub.talos import gen

TEMPLATES = resources.files("talkub") / "templates"
ALLOWED_EXISTING = {".git"}
_NAME = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?")


@dataclass(frozen=True)
class InitRequest:
    target: Path
    name: str
    endpoint: str
    git_url: str
    nodes: dict[str, NodeSpec]
    recipients: list[Recipient]


def render_node_patch(node: str) -> str:
    return Template((TEMPLATES / "node.yaml.tmpl").read_text()).substitute(node=node)


def init_cluster(request: InitRequest, runner: Runner) -> Repo:
    _validate(request)
    target = request.target
    existed = target.exists()
    if existed and {p.name for p in target.iterdir()} - ALLOWED_EXISTING:
        raise ValidationError(f"{target} is not empty", hint="use a new directory or a fresh git clone")

    # Network lookups first: nothing is written if they fail.
    chart_specs = charts.resolve_defaults(runner)
    target.mkdir(parents=True, exist_ok=True)
    try:
        return _populate(Repo(target.resolve()), request, chart_specs, runner)
    except BaseException:
        _cleanup(target, existed)
        raise


def _validate(request: InitRequest) -> None:
    if not _NAME.fullmatch(request.name):
        raise ValidationError(
            f"invalid cluster name {request.name!r}",
            hint="lowercase letters, digits and '-'",
        )
    if not request.endpoint.startswith("https://"):
        raise ValidationError("endpoint must start with https://", hint="e.g. https://10.0.0.10:6443")
    if not any(n.role == "controlplane" for n in request.nodes.values()):
        raise ValidationError("at least one controlplane node is required")
    if not request.recipients:
        raise ValidationError("at least one recipient is required")


def _cleanup(target: Path, existed: bool) -> None:
    if not existed:
        shutil.rmtree(target, ignore_errors=True)
        return
    for child in target.iterdir():
        if child.name in ALLOWED_EXISTING:
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


def _populate(repo: Repo, request: InitRequest, chart_specs, runner: Runner) -> Repo:
    keyring = Keyring(repo, runner, env={})
    keyring.create(request.recipients)
    public = keyring.public_key()
    sops = Sops(runner, keyring.master_key)
    atomic_write(repo.sops_config, render_sops_config(public).encode())

    controlplane_ips = [n.ip for n in request.nodes.values() if n.role == "controlplane"]
    with private_tempdir() as work:
        secrets = gen.gen_secrets(runner, work)
        defaults = gen.detect_defaults(runner, request.name, request.endpoint, secrets)
        talosconfig = gen.gen_talosconfig(
            runner,
            request.name,
            request.endpoint,
            secrets,
            work,
            endpoints=controlplane_ips,
            node=controlplane_ips[0],
        )
        for source, target in (
            (secrets, repo.talos_secrets),
            (talosconfig, repo.talosconfig),
        ):
            atomic_write(
                target,
                sops.encrypt(source.read_bytes(), filename=target.name, public_key=public),
            )

    settings = Settings(
        name=request.name,
        endpoint=request.endpoint,
        talos_version=defaults.talos_version,
        kubernetes_version=defaults.kubernetes_version,
        install_image=defaults.install_image,
        nodes=dict(request.nodes),
        charts=chart_specs,
    )
    repo.write_settings(settings)
    _write_tree(TEMPLATES / "repo", repo.root, _template_values(settings, request.git_url))
    for node in request.nodes:
        atomic_write(repo.talos_dir / "nodes" / f"{node}.yaml", render_node_patch(node).encode())
    return repo


def _template_values(settings: Settings, git_url: str) -> dict[str, str]:
    values = {"name": settings.name, "git_url": git_url}
    for key, spec in settings.charts.items():
        prefix = key.replace("-", "_")
        values |= {
            f"{prefix}_repo": spec.repo,
            f"{prefix}_chart": spec.chart,
            f"{prefix}_version": spec.version,
        }
    return values


def _write_tree(source: Traversable, destination: Path, values: dict[str, str]) -> None:
    """Copy templates; 'dot-x' becomes '.x', '*.tmpl' files are rendered with string.Template."""
    for entry in source.iterdir():
        name = "." + entry.name[4:] if entry.name.startswith("dot-") else entry.name
        if entry.is_dir():
            _write_tree(entry, destination / name, values)
        elif name.endswith(".tmpl"):
            text = Template(entry.read_text()).substitute(values)
            atomic_write(destination / name.removesuffix(".tmpl"), text.encode())
        else:
            atomic_write(destination / name, entry.read_bytes())
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_scaffold.py -v`
Expected: 7 passed. If `test_generated_patches_validate_with_talosctl` fails, print `result.stdout` and fix the patch template rather than weakening the assertion.

- [ ] **Step 6: Commit**

```bash
git add src/talkub/scaffold.py src/talkub/templates tests/test_scaffold.py
git commit -m "feat: init_cluster scaffolds a complete cluster repo

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: Key rotation and repo checks

**Files:**
- Modify: `src/talkub/keyring.py` (add `rotate`)
- Create: `src/talkub/checks.py`
- Test: `tests/test_rotate.py`, `tests/test_checks.py`

**Interfaces:**
- Consumes: `Keyring`, `Sops`, `render_sops_config`, `Repo.sops_files`, `yamlio`.
- Produces:
  - `Keyring.rotate(sops: Sops) -> None`
  - `checks.Finding(level: str, message: str)` with `level` in `{"error", "warning"}`
  - `checks.run_checks(repo: Repo) -> list[Finding]`

- [ ] **Step 1: Write the failing tests**

`tests/test_rotate.py`:

```python
import pytest

from helpers import need
from talkub.context import Context
from talkub.crypto.sops import Sops
from talkub.errors import DecryptFailed, ValidationError
from talkub.runner import Runner

pytestmark = need("age", "age-keygen", "sops")


def test_rotate_reencrypts_everything(repo_with_configs):
    ctx = Context.open(repo_with_configs)
    old_key = ctx.keyring.master_key()
    old_pub = ctx.keyring.public_key()

    ctx.keyring.rotate(ctx.sops)

    fresh = Context.open(repo_with_configs)
    new_key = fresh.keyring.master_key()
    assert new_key != old_key
    assert fresh.keyring.public_key() != old_pub
    assert fresh.keyring.public_key() in repo_with_configs.sops_config.read_text()
    assert fresh.sops.decrypt(repo_with_configs.talosconfig) == b"talos: marker\n"
    with pytest.raises(DecryptFailed):
        Sops(Runner(), lambda: old_key).decrypt(repo_with_configs.talosconfig)
    assert not repo_with_configs.master_next_age.exists()


def test_interrupted_rotation_blocks_new_one(repo_with_configs):
    repo_with_configs.master_next_age.write_text("leftover")
    ctx = Context.open(repo_with_configs)
    with pytest.raises(ValidationError) as exc:
        ctx.keyring.rotate(ctx.sops)
    assert "master.next.age" in exc.value.hint
```

`tests/test_checks.py`:

```python
from helpers import need
from talkub.checks import run_checks
from talkub.recipients import Recipient
from talkub.keyring import Keyring
from talkub.runner import Runner

pytestmark = need("age", "age-keygen", "sops")


def _messages(repo, level):
    return [f.message for f in run_checks(repo) if f.level == level]


def test_clean_repo_has_only_single_recipient_warning(repo_with_configs):
    assert _messages(repo_with_configs, "error") == []
    assert any("recipient" in m for m in _messages(repo_with_configs, "warning"))


def test_two_recipients_no_warning(repo_with_configs):
    Keyring(repo_with_configs, Runner()).add(
        Recipient("backup", "age1qk2vu7xxezxcfhyhasnx8c7z5j479xwevu5tqq8tc2fjqjzdegjqvepymq")
    )
    assert _messages(repo_with_configs, "warning") == []


def test_unencrypted_sops_file(repo_with_configs):
    (repo_with_configs.root / "talos" / "extra.sops.yaml").write_text("password: hunter2\n")
    assert any("extra.sops.yaml" in m for m in _messages(repo_with_configs, "error"))


def test_plain_secret_in_gitops(repo_with_configs):
    path = repo_with_configs.gitops_dir / "manifests" / "app" / "secret.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("apiVersion: v1\nkind: ConfigMap\n---\napiVersion: v1\nkind: Secret\nmetadata:\n  name: x\n")
    assert any("secret.yaml" in m and "Secret" in m for m in _messages(repo_with_configs, "error"))


def test_age_secret_key_anywhere(repo_with_configs):
    (repo_with_configs.root / "notes.txt").write_text("AGE-SECRET-KEY-1ABC\n")
    assert any("notes.txt" in m for m in _messages(repo_with_configs, "error"))


def test_known_plaintext_names(repo_with_configs):
    (repo_with_configs.root / "kubernetes" / "kubeconfig").write_text("x")
    assert any("kubernetes/kubeconfig" in m for m in _messages(repo_with_configs, "error"))


def test_interrupted_rotation_warned(repo_with_configs):
    repo_with_configs.master_next_age.write_text("x")
    assert any("master.next.age" in m for m in _messages(repo_with_configs, "warning"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_rotate.py tests/test_checks.py -v`
Expected: `AttributeError: 'Keyring' object has no attribute 'rotate'` and `ModuleNotFoundError: No module named 'talkub.checks'`.

- [ ] **Step 3: Add `rotate` to `src/talkub/keyring.py`**

Add these imports at the top of `keyring.py`:

```python
from talkub.crypto.sops import Sops, render_sops_config
```

Add this method to `Keyring` (after `remove`):

```python
def rotate(self, sops: Sops) -> None:
    """Replace the master key and re-encrypt every SOPS file.

    The new key is written to master.next.age before any SOPS file changes, so
    an interruption never loses the key those files are encrypted to.
    """
    pending = self.repo.master_next_age
    if pending.exists():
        raise ValidationError(
            "a previous key rotation was interrupted",
            hint=(
                "keys/master.next.age holds the newer key; decrypt it with `age -d`, "
                "check which key the SOPS files use, finish manually and delete master.next.age"
            ),
        )
    recipients = self.recipients()
    plaintexts = {path: sops.decrypt(path) for path in self.repo.sops_files()}
    new = age.generate(self.runner)

    atomic_write(
        pending,
        age.encrypt(self.runner, (new.secret + "\n").encode(), [r.key for r in recipients]),
    )
    for path, plaintext in plaintexts.items():
        atomic_write(path, sops.encrypt(plaintext, filename=path.name, public_key=new.public))
    atomic_write(self.repo.master_pub, (new.public + "\n").encode())
    atomic_write(self.repo.sops_config, render_sops_config(new.public).encode())
    os.replace(pending, self.repo.master_age)
    self._master = new.secret
```

- [ ] **Step 4: Implement `checks.py`**

`src/talkub/checks.py`:

```python
"""`talkub check`: guard rails before committing a cluster repo."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ruamel.yaml.error import YAMLError

from talkub import recipients as recipients_file
from talkub import yamlio
from talkub.errors import ValidationError
from talkub.repo import Repo

PLAINTEXT_NAMES = ("talos/secrets.yaml", "talos/talosconfig", "kubernetes/kubeconfig")


@dataclass(frozen=True)
class Finding:
    level: str  # "error" or "warning"
    message: str


def _repo_files(repo: Repo) -> list[Path]:
    return [path for path in repo.root.rglob("*") if path.is_file() and ".git" not in path.relative_to(repo.root).parts]


def run_checks(repo: Repo) -> list[Finding]:
    findings: list[Finding] = []

    for path in repo.sops_files():
        try:
            data = yamlio.load(path.read_text())
        except YAMLError:
            data = None
        if not isinstance(data, Mapping) or "sops" not in data:
            findings.append(Finding("error", f"{repo.rel(path)} is not SOPS-encrypted"))

    if repo.gitops_dir.is_dir():
        for path in sorted(repo.gitops_dir.rglob("*")):
            if path.suffix not in (".yaml", ".yml") or not path.is_file():
                continue
            try:
                docs = yamlio.load_all(path.read_text())
            except YAMLError:
                findings.append(Finding("error", f"{repo.rel(path)} is not valid YAML"))
                continue
            if any(isinstance(d, Mapping) and d.get("kind") == "Secret" for d in docs):
                findings.append(
                    Finding(
                        "error",
                        f"{repo.rel(path)} contains a plain Secret; seal it with kubeseal",
                    )
                )

    for path in _repo_files(repo):
        if b"AGE-SECRET-KEY-" in path.read_bytes():
            findings.append(Finding("error", f"{repo.rel(path)} contains an age private key"))

    for rel in PLAINTEXT_NAMES:
        if (repo.root / rel).exists():
            findings.append(Finding("error", f"{rel} is plaintext and must not be in the repo"))

    try:
        count = len(recipients_file.parse(repo.recipients_file.read_text()))
    except (OSError, ValidationError):
        findings.append(Finding("error", "keys/recipients.txt is missing or invalid"))
    else:
        if count < 2:
            findings.append(
                Finding(
                    "warning",
                    f"only {count} recipient(s); add a second admin or backup key",
                )
            )

    if repo.master_next_age.exists():
        findings.append(Finding("warning", "keys/master.next.age exists: a key rotation was interrupted"))

    return findings
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_rotate.py tests/test_checks.py -v`
Expected: all passed.

- [ ] **Step 6: Commit**

```bash
git add src/talkub/keyring.py src/talkub/checks.py tests/test_rotate.py tests/test_checks.py
git commit -m "feat: master key rotation and repo checks

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 11: CLI entry point, pass-through and session commands

**Files:**
- Create: `src/talkub/cli/__init__.py` (empty), `src/talkub/cli/state.py`, `src/talkub/cli/main.py`, `src/talkub/cli/session.py`, `src/talkub/cli/sops.py`
- Test: `tests/test_cli_main.py`

**Interfaces:**
- Consumes: `access.*`, `Context`, `find_repo`, `TalkubError`, `Sops.edit`/`decrypt`.
- Produces:
  - `cli.state.State(repo_path: Path | None = None, debug: bool = False)` with `repo() -> Repo` and `context() -> Context`
  - `cli.main.app` (Typer), `cli.main.main(argv: list[str] | None = None) -> None`, `cli.main.split_globals(argv) -> tuple[State, list[str]]`
  - `cli.session.register(app)`, `cli.sops.app`

- [ ] **Step 1: Write the failing tests**

`tests/test_cli_main.py`:

```python
import pytest

from helpers import need
from talkub.cli.main import main, split_globals

pytestmark = need("age", "age-keygen", "sops")

ECHO_ARGS = "printf 'args:%s\\n' \"$*\"\nexit 3\n"


def _run(argv):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    return exc.value.code


def test_split_globals():
    state, rest = split_globals(["--debug", "--repo", "/x", "k", "--repo", "y"])
    assert state.debug and str(state.repo_path) == "/x"
    assert rest == ["k", "--repo", "y"]


@pytest.mark.parametrize("alias", ["t", "talosctl"])
def test_talosctl_alias_passes_everything_verbatim(repo_with_configs, fake_bin, capfd, alias):
    fake_bin("talosctl", ECHO_ARGS)
    code = _run(["--repo", str(repo_with_configs.root), alias, "--help", "get", "--", "x"])
    assert code == 3
    assert "args:--help get -- x" in capfd.readouterr().out


def test_kubectl_exec_double_dash(repo_with_configs, fake_bin, capfd):
    fake_bin("kubectl", ECHO_ARGS)
    _run(
        [
            "--repo",
            str(repo_with_configs.root),
            "k",
            "exec",
            "-it",
            "pod",
            "--",
            "sh",
            "-c",
            "id",
        ]
    )
    assert "args:exec -it pod -- sh -c id" in capfd.readouterr().out


def test_repo_found_from_cwd(repo_with_configs, fake_bin, capfd, monkeypatch):
    fake_bin("kubectl", ECHO_ARGS)
    monkeypatch.chdir(repo_with_configs.root / "talos")
    assert _run(["k", "version"]) == 3


def test_errors_are_reported_without_traceback(tmp_path, capfd, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert _run(["k", "get", "pods"]) == 1
    err = capfd.readouterr().err
    assert err.startswith("error: no .talkub.yaml found")
    assert "hint:" in err
    assert "Traceback" not in err


def test_help_lists_commands(capfd):
    assert _run(["--help"]) == 0
    out = capfd.readouterr().out
    for command in ("talosctl", "kubectl", "shell", "sops"):
        assert command in out


def test_sops_decrypt_to_stdout(repo_with_configs, capfd):
    code = _run(
        [
            "--repo",
            str(repo_with_configs.root),
            "sops",
            "decrypt",
            str(repo_with_configs.talosconfig),
        ]
    )
    assert code == 0
    assert "talos: marker" in capfd.readouterr().out


def test_shell_command(repo_with_configs, fake_bin, capfd):
    program = fake_bin("fakeshell", "printf 'session:%s\\n' \"$TALKUB_SESSION\"\n")
    assert _run(["--repo", str(repo_with_configs.root), "shell", "--program", str(program)]) == 0
    assert "session:test" in capfd.readouterr().out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli_main.py -v`
Expected: `ModuleNotFoundError: No module named 'talkub.cli'`.

- [ ] **Step 3: Implement**

`src/talkub/cli/__init__.py`: empty file.

`src/talkub/cli/state.py`:

```python
"""Global CLI options shared by all commands."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from talkub.context import Context
from talkub.repo import Repo, find_repo


@dataclass
class State:
    repo_path: Path | None = None
    debug: bool = False

    def repo(self) -> Repo:
        return find_repo(Path.cwd(), self.repo_path)

    def context(self) -> Context:
        return Context.open(self.repo())
```

`src/talkub/cli/session.py`:

```python
"""shell, plus help-only stubs for the talosctl/kubectl pass-through.

The real pass-through happens in main.main() before Typer parses anything,
because Typer would swallow `--` and intercept `--help`.
"""

from __future__ import annotations

import typer

from talkub import access

PASSTHROUGH = {
    "allow_extra_args": True,
    "ignore_unknown_options": True,
    "help_option_names": [],
}


def register(app: typer.Typer) -> None:
    @app.command("talosctl", context_settings=PASSTHROUGH)
    def talosctl_cmd(ctx: typer.Context) -> None:
        """Run talosctl with the decrypted talosconfig (alias: t)."""
        raise typer.Exit(access.talosctl(ctx.obj.context(), ctx.args))

    @app.command("t", context_settings=PASSTHROUGH, hidden=True)
    def t_cmd(ctx: typer.Context) -> None:
        raise typer.Exit(access.talosctl(ctx.obj.context(), ctx.args))

    @app.command("kubectl", context_settings=PASSTHROUGH)
    def kubectl_cmd(ctx: typer.Context) -> None:
        """Run kubectl with the decrypted kubeconfig (alias: k)."""
        raise typer.Exit(access.kubectl(ctx.obj.context(), ctx.args))

    @app.command("k", context_settings=PASSTHROUGH, hidden=True)
    def k_cmd(ctx: typer.Context) -> None:
        raise typer.Exit(access.kubectl(ctx.obj.context(), ctx.args))

    @app.command("shell")
    def shell_cmd(
        ctx: typer.Context,
        program: str = typer.Option(None, "--program", help="Shell to start (default: $SHELL)."),
    ) -> None:
        """Start a subshell with TALOSCONFIG and KUBECONFIG set; removed on exit."""
        raise typer.Exit(access.shell(ctx.obj.context(), program))
```

`src/talkub/cli/sops.py`:

```python
"""sops edit / decrypt with the resolved master key."""

from __future__ import annotations

import sys
from pathlib import Path

import typer

app = typer.Typer(help="Edit or decrypt SOPS files with the cluster master key.", no_args_is_help=True)


@app.command("edit")
def edit(ctx: typer.Context, file: Path) -> None:
    """Open a SOPS file in $EDITOR (creates it if missing)."""
    context = ctx.obj.context()
    raise typer.Exit(context.sops.edit(file.resolve(), cwd=context.repo.root))


@app.command("decrypt")
def decrypt(ctx: typer.Context, file: Path) -> None:
    """Print the decrypted content to stdout."""
    sys.stdout.buffer.write(ctx.obj.context().sops.decrypt(file.resolve()))
    sys.stdout.buffer.flush()
```

`src/talkub/cli/main.py`:

```python
"""Entry point: global options, error reporting and raw pass-through for t/k."""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

import typer

from talkub import __version__, access
from talkub.cli import session
from talkub.cli import sops as sops_cli
from talkub.cli.state import State
from talkub.errors import TalkubError

app = typer.Typer(
    help="Administer a Talos cluster repo (ArgoCD, SOPS/age, Sealed Secrets).",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)


def _version(value: bool) -> None:
    if value:
        print(__version__)
        raise typer.Exit()


@app.callback()
def _globals(
    ctx: typer.Context,
    repo: Path = typer.Option(None, "--repo", help="Cluster repo (default: search upwards for .talkub.yaml)."),
    debug: bool = typer.Option(False, "--debug", help="Show tracebacks on errors."),
    version: bool = typer.Option(False, "--version", callback=_version, is_eager=True, help="Show the version."),
) -> None:
    ctx.obj = State(repo_path=repo, debug=debug)


session.register(app)
app.add_typer(sops_cli.app, name="sops")

PASSTHROUGH = {
    "t": access.talosctl,
    "talosctl": access.talosctl,
    "k": access.kubectl,
    "kubectl": access.kubectl,
}


def split_globals(argv: list[str]) -> tuple[State, list[str]]:
    """Consume leading --repo/--debug; the rest belongs to the subcommand."""
    state = State()
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--debug":
            state.debug = True
            index += 1
        elif arg == "--repo" and index + 1 < len(argv):
            state.repo_path = Path(argv[index + 1])
            index += 2
        elif arg.startswith("--repo="):
            state.repo_path = Path(arg.split("=", 1)[1])
            index += 1
        else:
            break
    return state, argv[index:]


def _report(err: TalkubError, debug: bool) -> None:
    print(f"error: {err}", file=sys.stderr)
    if err.hint:
        print(f"hint: {err.hint}", file=sys.stderr)
    if debug:
        traceback.print_exc()


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    state, rest = split_globals(argv)
    try:
        if rest and rest[0] in PASSTHROUGH:
            raise SystemExit(PASSTHROUGH[rest[0]](state.context(), rest[1:]))
        app(args=argv, prog_name="talkub")
    except TalkubError as err:
        _report(err, state.debug)
        raise SystemExit(1) from None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli_main.py -v`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add src/talkub/cli tests/test_cli_main.py
git commit -m "feat: CLI entry point with verbatim talosctl/kubectl pass-through

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 12: CLI management commands (identity, init, admin, keys, check)

**Files:**
- Create: `src/talkub/cli/setup.py`, `src/talkub/cli/admin.py`, `src/talkub/cli/keys.py`, `src/talkub/cli/check.py`
- Modify: `src/talkub/cli/main.py` (register the new commands)
- Test: `tests/test_cli_manage.py`

**Interfaces:**
- Consumes: `identity.create_identity`, `identity.identities_dir`, `identity.own_recipients`, `scaffold.InitRequest`/`init_cluster`, `Keyring`, `checks.run_checks`, `recipients.Recipient`/`validate_key`/`validate_name`, `NodeSpec`.
- Produces: `cli.setup.identity_app`, `cli.setup.init`, `cli.admin.app`, `cli.keys.app`, `cli.check.check`.

- [ ] **Step 1: Write the failing tests**

`tests/test_cli_manage.py`:

```python
import os

import pytest

from helpers import need
from talkub.cli.main import main
from talkub.identity import identities_dir

HELM = "printf 'apiVersion: v2\\nname: x\\nversion: 1.2.3\\n'\n"
OTHER_KEY = "age1qk2vu7xxezxcfhyhasnx8c7z5j479xwevu5tqq8tc2fjqjzdegjqvepymq"


def _run(argv):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    return exc.value.code


@need("age-keygen")
def test_identity_new_software(capfd):
    assert _run(["identity", "new", "--software"]) == 0
    out = capfd.readouterr().out
    assert "age1" in out
    assert (identities_dir(os.environ) / "software.txt").is_file()


def test_identity_new_rejects_two_kinds(capfd):
    assert _run(["identity", "new", "--software", "--yubikey"]) == 1
    assert "only one" in capfd.readouterr().err


@need("age", "age-keygen", "sops", "talosctl")
def test_init_then_admin_and_check(tmp_path, software_identity, fake_bin, capfd, monkeypatch):
    fake_bin("helm", HELM)
    monkeypatch.chdir(tmp_path)
    code = _run(
        [
            "init",
            "demo",
            "--endpoint",
            "https://10.0.0.10:6443",
            "--node",
            "cp-01=10.0.0.10",
            "--git-url",
            "https://git.example.com/demo.git",
        ]
    )
    assert code == 0
    assert (tmp_path / "demo" / ".talkub.yaml").is_file()
    monkeypatch.chdir(tmp_path / "demo")

    assert _run(["check"]) == 0
    assert "warning: only 1 recipient" in capfd.readouterr().out

    assert _run(["admin", "add", OTHER_KEY, "--name", "backup"]) == 0
    assert _run(["admin", "list"]) == 0
    out = capfd.readouterr().out
    assert "backup\tage1qk2vu7" in out

    assert _run(["admin", "remove", "backup"]) == 0
    assert "keys rotate" in capfd.readouterr().out


@need("age", "age-keygen", "sops")
def test_admin_remove_last_fails(keyed_repo, capfd):
    assert _run(["--repo", str(keyed_repo.root), "admin", "remove", "me"]) == 1
    assert "last recipient" in capfd.readouterr().err


@need("age", "age-keygen", "sops")
def test_check_fails_on_error(repo_with_configs, capfd):
    (repo_with_configs.root / "leak.txt").write_text("AGE-SECRET-KEY-1ABC\n")
    assert _run(["--repo", str(repo_with_configs.root), "check"]) == 1
    assert "error: leak.txt contains an age private key" in capfd.readouterr().out


@need("age", "age-keygen", "sops")
def test_keys_rotate_with_yes(repo_with_configs, capfd):
    old = repo_with_configs.master_pub.read_text()
    assert _run(["--repo", str(repo_with_configs.root), "keys", "rotate", "--yes"]) == 0
    assert repo_with_configs.master_pub.read_text() != old


def test_init_rejects_bad_node_syntax(tmp_path, capfd, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code = _run(
        [
            "init",
            "demo",
            "--endpoint",
            "https://x:6443",
            "--node",
            "cp-01",
            "--git-url",
            "https://g",
        ]
    )
    assert code == 1
    assert "name=ip" in capfd.readouterr().err
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli_manage.py -v`
Expected: failures with `No such command 'identity'` (exit code 2 instead of the expected codes).

- [ ] **Step 3: Implement**

`src/talkub/cli/setup.py`:

```python
"""identity new, init."""

from __future__ import annotations

import getpass
import os
from pathlib import Path

import typer

from talkub.errors import ValidationError
from talkub.identity import create_identity, identities_dir, own_recipients
from talkub.repo import NodeSpec
from talkub.runner import Runner
from talkub.scaffold import InitRequest, init_cluster

identity_app = typer.Typer(help="Manage your personal age identities.", no_args_is_help=True)


@identity_app.command("new")
def identity_new(
    software: bool = typer.Option(False, "--software", help="Plain age key file (default)."),
    yubikey: bool = typer.Option(False, "--yubikey", help="YubiKey via age-plugin-yubikey."),
    se: bool = typer.Option(False, "--se", help="macOS Secure Enclave via age-plugin-se."),
    name: str = typer.Option(None, "--name", help="File name (default: the kind)."),
) -> None:
    """Create an identity in ~/.config/talkub/identities and print its public key."""
    chosen = [kind for kind, flag in (("software", software), ("yubikey", yubikey), ("se", se)) if flag]
    if len(chosen) > 1:
        raise ValidationError("choose only one of --software, --yubikey, --se")
    path, public = create_identity(Runner(), (chosen or ["software"])[0], identities_dir(os.environ), name)
    print(f"identity: {path}")
    print(f"public key: {public}")
    print("Share the public key with an admin: talkub admin add <key> --name <you>")


def _parse_node(value: str) -> tuple[str, NodeSpec]:
    name, sep, ip = value.partition("=")
    if not sep or not name or not ip:
        raise ValidationError(f"invalid --node {value!r}", hint="use name=ip, e.g. cp-01=10.0.0.10")
    return name, NodeSpec(ip=ip)


def init(
    name: str = typer.Argument(..., help="Cluster name; also the directory name."),
    endpoint: str = typer.Option(..., "--endpoint", help="Kubernetes API endpoint, e.g. https://10.0.0.10:6443"),
    node: list[str] = typer.Option(..., "--node", help="Control plane node as name=ip (repeatable)."),
    git_url: str = typer.Option(..., "--git-url", help="Git URL ArgoCD pulls this repo from."),
    directory: Path = typer.Option(None, "--dir", help="Target directory (default: ./<name>)."),
) -> None:
    """Create a new cluster repo with keys, Talos secrets and the GitOps skeleton."""
    nodes = dict(_parse_node(value) for value in node)
    request = InitRequest(
        target=(directory or Path.cwd() / name),
        name=name,
        endpoint=endpoint,
        git_url=git_url,
        nodes=nodes,
        recipients=own_recipients(os.environ, getpass.getuser()),
    )
    repo = init_cluster(request, Runner())
    print(f"created {repo.root}")
    print("next: add a second admin or backup key (talkub admin add), then git add/commit")
```

`src/talkub/cli/admin.py`:

```python
"""admin add / remove / list."""

from __future__ import annotations

import typer

from talkub.recipients import Recipient, validate_key, validate_name

app = typer.Typer(help="Manage who can decrypt the cluster master key.", no_args_is_help=True)


@app.command("add")
def add(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="age public key (age1...)."),
    name: str = typer.Option(..., "--name", help="Admin name."),
) -> None:
    """Add a recipient and re-encrypt keys/master.age."""
    context = ctx.obj.context()
    context.keyring.add(Recipient(validate_name(name), validate_key(key)))
    print(f"added {name}; commit keys/ to share access")


@app.command("remove")
def remove(ctx: typer.Context, name: str = typer.Argument(..., help="Admin name.")) -> None:
    """Remove a recipient and re-encrypt keys/master.age."""
    ctx.obj.context().keyring.remove(name)
    print(f"removed {name}. They may still know the old master key: run `talkub keys rotate`.")


@app.command("list")
def list_(ctx: typer.Context) -> None:
    """List recipients."""
    items = ctx.obj.context().keyring.recipients()
    for item in items:
        print(f"{item.name}\t{item.key}")
    if len(items) < 2:
        print("warning: fewer than 2 recipients - losing one key locks you out")
```

`src/talkub/cli/keys.py`:

```python
"""keys rotate."""

from __future__ import annotations

import typer

app = typer.Typer(help="Manage the cluster master key.", no_args_is_help=True)


@app.command("rotate")
def rotate(
    ctx: typer.Context,
    yes: bool = typer.Option(False, "--yes", help="Do not ask for confirmation."),
) -> None:
    """Create a new master key and re-encrypt every *.sops.yaml."""
    context = ctx.obj.context()
    count = len(context.repo.sops_files())
    if not yes and not typer.confirm(f"Rotate the master key and re-encrypt {count} file(s)?"):
        raise typer.Exit(1)
    context.keyring.rotate(context.sops)
    print(f"rotated; re-encrypted {count} file(s). Commit all changes together.")
```

`src/talkub/cli/check.py`:

```python
"""check."""

from __future__ import annotations

import typer

from talkub.checks import run_checks


def check(ctx: typer.Context) -> None:
    """Verify encryption and secret hygiene (use as a pre-commit hook)."""
    findings = run_checks(ctx.obj.repo())
    for finding in findings:
        print(f"{finding.level}: {finding.message}")
    if any(f.level == "error" for f in findings):
        raise typer.Exit(1)
    if not findings:
        print("ok")
```

In `src/talkub/cli/main.py`, replace the import block line `from talkub.cli import session` and the registration lines so they read:

```python
from talkub.cli import admin, check, keys, session, setup
from talkub.cli import sops as sops_cli
```

```python
session.register(app)
app.add_typer(setup.identity_app, name="identity")
app.command("init")(setup.init)
app.add_typer(admin.app, name="admin")
app.add_typer(keys.app, name="keys")
app.add_typer(sops_cli.app, name="sops")
app.command("check")(check.check)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli_manage.py -v`
Expected: all passed.

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest -q`
Expected: all passed, no skips on this machine (age, sops, talosctl, helm installed).

- [ ] **Step 6: Commit**

```bash
git add src/talkub/cli tests/test_cli_manage.py
git commit -m "feat: identity, init, admin, keys and check commands

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 13: Packaging check and README

**Files:**
- Modify: `README.md`
- Test: manual verification commands (no new test file)

**Interfaces:**
- Consumes: the finished CLI.
- Produces: an installable wheel that contains the templates.

- [ ] **Step 1: Build and verify the wheel includes templates**

Run:

```bash
uv build
unzip -l dist/talkub-0.1.0-py3-none-any.whl | grep -E "templates/(node.yaml.tmpl|repo/dot-gitignore|repo/gitops/manifests/dot-gitkeep|repo/talos/patches/controlplane.yaml)"
```

Expected: all four paths listed. If `dot-gitkeep` is missing, add to `pyproject.toml`:

```toml
[tool.hatch.build.targets.wheel.force-include]
"src/talkub/templates" = "talkub/templates"
```

and rebuild.

- [ ] **Step 2: Install as a uv tool and smoke-test outside the repo**

Run:

```bash
uv tool install --force dist/talkub-0.1.0-py3-none-any.whl
cd "$(mktemp -d)" && talkub --version && talkub --help
```

Expected: `0.1.0`, then help listing `talosctl`, `kubectl`, `shell`, `identity`, `init`, `admin`, `keys`, `sops`, `check`.

- [ ] **Step 3: Write the README**

`README.md`:

````markdown
# talkub

Admin CLI for a Talos Linux cluster repo: ArgoCD app-of-apps, SOPS/age for
infrastructure secrets, Sealed Secrets for app secrets. The tool lives outside
the cluster repo and is installed with uv.

## Install

    uv tool install git+https://<your-git-host>/talkub.git
    # needs: age, sops, talosctl, helm (and kubectl, kubeseal later)
    # optional: age-plugin-yubikey, age-plugin-se

## Key model

- Every `*.sops.yaml` is encrypted to one **cluster master key**.
- `keys/master.age` holds that key, encrypted to all admins in `keys/recipients.txt`.
- Each admin has a personal identity (software key, YubiKey or Secure Enclave)
  in `~/.config/talkub/identities/`.
- Lookup order: `SOPS_AGE_KEY` / `SOPS_AGE_KEY_FILE` (the master key itself, for CI)
  → `TALKUB_IDENTITY` → `~/.config/talkub/identities/`.
- Keep at least two recipients (e.g. YubiKey + offline backup key). There is no passphrase recovery.

## Quick start

    talkub identity new --yubikey          # or --se / --software
    talkub init homelab --endpoint https://10.0.0.10:6443 \
        --node cp-01=10.0.0.10 --git-url https://git.example.com/homelab.git
    cd homelab
    talkub admin add age1... --name backup
    talkub check && git init && git add -A && git commit -m "init cluster repo"

## Daily use

    talkub t get members               # talosctl, config decrypted only for this call
    talkub k get pods -A               # kubectl
    talkub shell                       # subshell with TALOSCONFIG/KUBECONFIG, removed on exit
    talkub sops edit talos/secrets.sops.yaml

Inside `talkub shell`, `TALKUB_SESSION` holds the cluster name. zsh prompt example:

    PROMPT='${TALKUB_SESSION:+(talkub:$TALKUB_SESSION) }'$PROMPT

## Admin changes

    talkub admin remove alice && talkub keys rotate

## Development

    uv run pytest
````

- [ ] **Step 4: Commit**

```bash
git add README.md pyproject.toml
git commit -m "docs: README with install, key model and usage

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

## Out of scope for this plan (Phase 2 plan follows)

Rendering machine configs, `node add`, `plan`/`apply` (`--insecure` first install), `bootstrap`
(including Helm installs of sealed-secrets and ArgoCD from `gitops/values/`), `upgrade talos|k8s`,
`status`, `secret seal`, `secret backup-sealing-key`. Phase 2 reuses `talos/gen.py`,
`scaffold.render_node_patch`, `Settings.nodes/charts` and `access`/`ephemeral` from this plan.
