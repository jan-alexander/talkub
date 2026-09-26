# talkub Package Structure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorganize the flat `src/talkub/` into domain packages with an enforced dependency direction, without changing behaviour.

**Architecture:** First remove the three wrong-direction imports while the layout is still flat. Then move modules bottom-up (core → config → secrets → kube/talos → argocd/helm/quality/gitops → workstation/overview) with `scripts/move_modules.py`, which `git mv`s files and rewrites every import in `src/` and `tests/`. Split `repo.py` inside `config/`. Mirror the packages in `tests/`, then lock the layering with import-linter.

**Tech Stack:** Python 3.12, uv, pytest, ruff, import-linter (new dev dependency).

**Spec:** `docs/superpowers/specs/2026-09-26-talkub-package-structure-design.md`

## Global Constraints

- No behaviour or CLI changes. The full suite (922 tests at the start) must pass after every task.
- Code, comments, docstrings and commit messages in English.
- Run `uv run ruff check --fix --select I src tests scripts` and `uv run ruff format src tests scripts` before each commit. `uv run ruff check src tests` must report only the 30 pre-existing findings (16 B008, 11 PLW1510, 3 SIM117), no new ones.
- Move files only with `git mv` (the script does this), so `git log --follow` keeps history.
- Test file basenames must stay unique across `tests/` (pytest default import mode, no `__init__.py` in `tests/`).
- Internal modules of a package import siblings by submodule path (`from talkub.config.specs import NodeSpec`), never through the package's `__init__` re-exports. This avoids import cycles.
- The full suite takes ~5 minutes. Run it in the background or with a timeout of at least 600 s.
- `tests/test_runner.py::test_run_uninterruptible_*` send real signals and can fail when two suites run at the same time. Never run two suites in parallel; if only those two fail, rerun `uv run pytest tests/test_runner.py` on its own before treating it as a regression.

## Review Focus

1. `python -m talkub.workstation.clipboard_clear`: the detached clipboard helper is started by module path from a string in `clipboard.py`. If the string is not rewritten, copying a secret works but it is never cleared from the clipboard. Covered by `tests/test_clipboard.py` asserting the argv (Task 6).
2. String monkeypatch targets (`"talkub.rotation.finish"`) silently patch nothing if they point at a stale path. `monkeypatch.setattr` with a string raises on a missing module, so a green suite proves they were rewritten (Task 4).
3. `talkub init` loads `resources.files("talkub") / "templates"`. `scaffold.py` and `templates/` must stay directly under `talkub/`. Covered by `tests/test_scaffold.py`.
4. The installed wheel (`packages = ["src/talkub"]`) must contain the new subpackages. Covered by the build smoke check in Task 9.
5. `from talkub.config import X` re-exports: a name missing from `config/__init__.py` fails only at import time of the caller. Covered because every module is imported by the suite; Task 3 also imports each re-exported name explicitly.

---

### Task 1: Remove wrong-direction imports (flat layout)

**Files:**
- Create: `src/talkub/helm/__init__.py`, `src/talkub/helm/install.py`, `src/talkub/cli/output.py`, `tests/test_helm.py`
- Modify: `src/talkub/bootstrap.py:191-224` (remove `chart_ref`, `helm_install`), `src/talkub/coredns.py:276,361`, `src/talkub/chart_upgrade.py:54`, `src/talkub/lint.py:18,361`, `src/talkub/argoapps.py`, `src/talkub/gitrepo.py:172`, `src/talkub/checks.py:14`, `src/talkub/cli/tailscale.py:40-42`, `src/talkub/cli/hooks.py:8`, `src/talkub/cli/setup.py:22`
- Test: `tests/test_helm.py` (the test moves out of `tests/test_bootstrap.py:390-420`)

**Interfaces:**
- Produces: `talkub.helm.install.chart_ref(chart: ChartSpec) -> list[str]`, `talkub.helm.install.helm_install(kube: KubeSession, source: HelmSource, *, create_namespace: bool) -> None`, `talkub.argoapps.SECRET_TYPE_LABEL: str`, `talkub.cli.output.print_notes(notes: list[str]) -> None`

- [ ] **Step 1: Write the failing test**

Delete `test_helm_install_oci_chart` from `tests/test_bootstrap.py` (lines 390–420) and create `tests/test_helm.py`:

```python
from pathlib import Path

from helpers import FakeRunner

from talkub.argoapps import HelmSource
from talkub.helm.install import chart_ref, helm_install
from talkub.kube import KubeSession
from talkub.repo import ChartSpec


def test_chart_ref_oci_is_a_single_reference():
    chart = ChartSpec(repo="oci://ghcr.io/argoproj/argo-helm/", chart="argo-cd", version="1")
    assert chart_ref(chart) == ["oci://ghcr.io/argoproj/argo-helm/argo-cd"]


def test_chart_ref_https_uses_repo_flag():
    chart = ChartSpec(repo="https://charts.example.org", chart="demo", version="1")
    assert chart_ref(chart) == ["demo", "--repo", "https://charts.example.org"]


def test_helm_install_oci_chart(tmp_path):
    runner = FakeRunner()
    source = HelmSource(
        app="argocd",
        release="argocd",
        namespace="argocd",
        repo="oci://ghcr.io/argoproj/argo-helm/",
        chart="argo-cd",
        version="10.9.2",
        value_files=(tmp_path / "argocd.yaml",),
    )
    helm_install(KubeSession(runner, Path("kc")), source, create_namespace=True)
    [call] = runner.calls
    assert call.argv[:5] == [
        "helm",
        "upgrade",
        "--install",
        "argocd",
        "oci://ghcr.io/argoproj/argo-helm/argo-cd",
    ]
    assert "--repo" not in call.argv
    assert call.argv[call.argv.index("--version") + 1] == "10.9.2"
    assert call.argv[call.argv.index("-f") + 1] == str(tmp_path / "argocd.yaml")
    assert call.argv[-1] == "--create-namespace"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_helm.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'talkub.helm'`

- [ ] **Step 3: Create `talkub.helm.install`**

`src/talkub/helm/__init__.py`:

```python
"""Helm: the chart catalog talkub ships and the `helm` invocations it runs."""
```

`src/talkub/helm/install.py`: move `chart_ref` and `helm_install` out of `bootstrap.py` unchanged (cut lines 191–224):

```python
"""`helm` command lines: naming a chart and installing an Argo CD app's chart source directly."""

from __future__ import annotations

from contextlib import ExitStack

from talkub.argoapps import HelmSource
from talkub.ephemeral import private_file
from talkub.kube import KubeSession
from talkub.repo import ChartSpec


def chart_ref(chart: ChartSpec) -> list[str]:
    """helm arguments naming the chart: an OCI reference, or chart name plus --repo."""
    if chart.repo.startswith("oci://"):
        return [f"{chart.repo.rstrip('/')}/{chart.chart}"]
    return [chart.chart, "--repo", chart.repo]


def helm_install(kube: KubeSession, source: HelmSource, *, create_namespace: bool) -> None:
    """`helm upgrade --install` one Argo CD app's chart source as Argo CD would render it."""
    chart = ChartSpec(repo=source.repo, chart=source.chart, version=source.version)
    argv = [
        "upgrade",
        "--install",
        source.release,
        *chart_ref(chart),
        "--version",
        source.version,
        "-n",
        source.namespace,
    ]
    for value_file in source.value_files:
        argv += ["-f", str(value_file)]
    with ExitStack() as stack:
        if source.inline_values is not None:
            inline = stack.enter_context(
                private_file("values.yaml", source.inline_values.encode())
            )
            argv += ["-f", str(inline)]
        argv += ["--wait", "--timeout", "10m"]
        if create_namespace:
            argv.append("--create-namespace")
        kube.helm(argv)
```

- [ ] **Step 4: Point the callers at `talkub.helm.install`**

- `bootstrap.py`: add `from talkub.helm.install import helm_install`. Calls at lines 35 and 101 stay `helm_install(...)`. Remove `ExitStack` and `private_file` imports if they become unused (ruff F401 tells you).
- `coredns.py:276`: `bootstrap.helm_install(` → `helm_install(`. `coredns.py:361`: `*bootstrap.chart_ref(chart)` → `*chart_ref(chart)`. Add `from talkub.helm.install import chart_ref, helm_install`. If `bootstrap` is then unused in `coredns.py`, drop it from `from talkub import apps, argoapps, bootstrap, charts, lifecycle, yamlio`.
- `chart_upgrade.py:54` and `lint.py:361`: `*bootstrap.chart_ref(chart)` → `*chart_ref(chart)`, add `from talkub.helm.install import chart_ref`, drop `bootstrap` from their `from talkub import ...` lines (`lint.py:18` imports only `bootstrap`, so delete that line).

Check: `grep -rn "bootstrap\.\(chart_ref\|helm_install\)" src tests` prints nothing.

- [ ] **Step 5: Move `SECRET_TYPE_LABEL` to `argoapps.py`**

Cut `SECRET_TYPE_LABEL = "argocd.argoproj.io/secret-type"` from `gitrepo.py:172` and paste it into `argoapps.py` below the imports, with the comment `# Label that marks a Secret as Argo CD configuration (repository, cluster, ...).` In `gitrepo.py` add `from talkub.argoapps import SECRET_TYPE_LABEL`. In `checks.py:14` replace `from talkub.gitrepo import SECRET_TYPE_LABEL` with `from talkub.argoapps import SECRET_TYPE_LABEL`.

- [ ] **Step 6: Move `print_notes` to `cli/output.py`**

`src/talkub/cli/output.py`:

```python
"""Console output shared by CLI commands."""

from __future__ import annotations


def print_notes(notes: list[str]) -> None:
    for note in notes:
        print(f"note: {note}")
```

Delete `print_notes` from `cli/tailscale.py` and add `from talkub.cli.output import print_notes` there. `cli/hooks.py:8` → `from talkub.cli.output import print_notes`. `cli/setup.py:22` → `from talkub.cli.output import print_notes` plus `from talkub.cli.tailscale import read_authkey`.

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/test_helm.py -v` → 3 passed.
Run: `uv run pytest -q` → all pass (922 − 1 moved + 3 new = 924).

- [ ] **Step 8: Commit**

```bash
uv run ruff check --fix --select I src tests && uv run ruff format src tests
git add -A src tests
git commit -m "refactor: move helm helpers, secret-type label and print_notes out of flows"
```

---

### Task 2: `core/`, `config/` and `secrets/` packages

**Files:**
- Add to git: `scripts/move_modules.py` (already in the working tree, untracked; removed again in Task 9)
- Move: `errors, runner, fsutil, yamlio, ephemeral, findings` → `core/`; `repo, schematic` → `config/`; `crypto/age, crypto/sops, identity, keyring, recipients` → `secrets/`
- Delete: `src/talkub/crypto/__init__.py`
- Modify: every importer (the script rewrites them)

**Interfaces:**
- Produces: `talkub.core.{errors,runner,fsutil,yamlio,ephemeral,findings}`, `talkub.config.repo` (still the whole old `repo.py`), `talkub.config.schematic`, `talkub.secrets.{age,sops,identity,keyring,recipients}`

- [ ] **Step 1: Read the script**

Read `scripts/move_modules.py` (docstring at the top). `OLD=NEW` are dotted paths relative to `talkub`. It creates missing `__init__.py` files, runs `git mv`, and rewrites `from talkub[.pkg] import ...` statements and `"talkub.X"` string literals in `src/` and `tests/`.

- [ ] **Step 2: Move the modules**

```bash
git add scripts/move_modules.py
uv run python scripts/move_modules.py \
  errors=core.errors runner=core.runner fsutil=core.fsutil yamlio=core.yamlio \
  ephemeral=core.ephemeral findings=core.findings \
  repo=config.repo schematic=config.schematic \
  crypto.age=secrets.age crypto.sops=secrets.sops identity=secrets.identity \
  keyring=secrets.keyring recipients=secrets.recipients
git rm -q src/talkub/crypto/__init__.py
uv run ruff check --fix --select I src tests scripts && uv run ruff format src tests scripts
```

Check: `grep -rnE "talkub\.(errors|runner|fsutil|yamlio|ephemeral|findings|repo|schematic|crypto|identity|keyring|recipients)\b" src tests` prints nothing.

- [ ] **Step 3: Write the package docstrings**

Replace the empty `__init__.py` files:
- `src/talkub/core/__init__.py`: `"""Domain-free building blocks: errors, running tools, YAML, atomic writes, temp files."""`
- `src/talkub/config/__init__.py`: `"""The cluster repo on disk: locating it and reading/writing .talkub.yaml."""`
- `src/talkub/secrets/__init__.py`: `"""Local key material: age identities, the repo's recipients, the master key and SOPS."""`

- [ ] **Step 4: Run the tests**

Run: `uv run python -c "import talkub.cli.main"` → no output.
Run: `uv run pytest -q` → 924 passed.
Run: `uv run ruff check src tests --statistics` → only the 30 pre-existing findings.

- [ ] **Step 5: Commit**

```bash
git add -A src tests scripts
git commit -m "refactor: group core, config and secrets modules into packages"
```

---

### Task 3: Split `config/repo.py`

**Files:**
- Create: `src/talkub/config/fields.py`, `src/talkub/config/specs.py`, `src/talkub/config/shortcuts.py`, `src/talkub/config/settings.py`
- Modify: `src/talkub/config/repo.py` (keeps only `MARKER`, `Repo`, `find_repo`), `src/talkub/config/__init__.py` (re-exports), every `from talkub.config.repo import ...` in `src/` and `tests/`
- Test: `tests/test_config_api.py`

**Interfaces:**
- Consumes: `talkub.config.repo` from Task 2
- Produces: `from talkub.config import MARKER, ROLES, PLATFORMS, ARCHES, DNS_LABEL, Repo, find_repo, Settings, NodeSpec, ChartSpec, SchematicSpec, TailscaleSpec, GitSpec, Shortcut, valid_label, parse_installer_image`

Content of each new module (cut from `config/repo.py` unchanged except the renames listed):

| Module | Content from `repo.py` | Renames |
|---|---|---|
| `fields.py` | `DNS_LABEL`, `valid_label`, `_check_choice`, `_optional_text`, `_text_list`, `_label` | `_check_choice` → `check_choice`, `_optional_text` → `optional_text`, `_text_list` → `text_list`, `_label` → `label` (package-internal helpers, used by three modules now) |
| `specs.py` | `ROLES`, `PLATFORMS`, `ARCHES`, `_INSTALLER`, `NodeSpec`, `ChartSpec`, `SchematicSpec`, `TailscaleSpec`, `GitSpec`, `parse_installer_image`, `_schematic_dict`, `_node_dict` | `_schematic_dict` → `schematic_to_dict`, `_node_dict` → `node_to_dict` |
| `shortcuts.py` | `_SECRET_KEY`, `_RUN_TOOLS`, `_SHORTCUT_KINDS`, `_PORT_SPEC`, `_valid_port_spec`, `Shortcut`, `_shortcut_from_dict`, `_shortcut_dict` | `_shortcut_from_dict` → `shortcut_from_dict`, `_shortcut_dict` → `shortcut_to_dict` |
| `settings.py` | `Settings` | — |
| `repo.py` | `MARKER`, `Repo`, `find_repo` | — |

Module docstrings:
- `fields.py`: `"""Validation helpers for .talkub.yaml fields, shared by the spec parsers."""`
- `specs.py`: `"""Value objects in .talkub.yaml: nodes, charts, schematics, Tailscale and git settings."""`
- `shortcuts.py`: `"""`shortcuts:` entries in .talkub.yaml: parsing and serializing them."""`
- `settings.py`: `"""`Settings`: the parsed .talkub.yaml as a whole."""`
- `repo.py` keeps its docstring: `"""Locating a cluster repo and reading/writing its settings (.talkub.yaml)."""`

- [ ] **Step 1: Write the failing test**

`tests/test_config_api.py`:

```python
import talkub.config as config

PUBLIC = (
    "MARKER ROLES PLATFORMS ARCHES DNS_LABEL Repo find_repo Settings NodeSpec ChartSpec "
    "SchematicSpec TailscaleSpec GitSpec Shortcut valid_label parse_installer_image"
).split()


def test_config_package_reexports_public_api():
    missing = [name for name in PUBLIC if not hasattr(config, name)]
    assert missing == []


def test_repo_module_only_locates_and_persists():
    from talkub.config import repo

    assert not hasattr(repo, "NodeSpec")
    assert not hasattr(repo, "Shortcut")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_config_api.py -v`
Expected: both FAIL (`missing == [...]`, and `repo` still has `NodeSpec`).

- [ ] **Step 3: Split the module**

Create the four modules per the table. Each imports only what it uses, from sibling submodules:
- `specs.py`: `from talkub.config.fields import check_choice` (if used) and `from talkub.config.schematic import FACTORY_HOST, customization_hash`
- `shortcuts.py`: `from talkub.config.fields import label, optional_text, text_list, valid_label` (whatever `_shortcut_from_dict` uses)
- `settings.py`: `from talkub.config.fields import check_choice, optional_text, text_list`, `from talkub.config.specs import ...`, `from talkub.config.shortcuts import Shortcut, shortcut_from_dict, shortcut_to_dict`
- `repo.py`: `from talkub.config.settings import Settings`

Update the renamed calls inside the moved code (`_check_choice(` → `check_choice(`, and so on). Use `uv run ruff check src/talkub/config` (F821 undefined name, F401 unused import) to find what is missing.

`src/talkub/config/__init__.py`:

```python
"""The cluster repo on disk: locating it and reading/writing .talkub.yaml."""

from talkub.config.fields import DNS_LABEL, valid_label
from talkub.config.repo import MARKER, Repo, find_repo
from talkub.config.settings import Settings
from talkub.config.shortcuts import Shortcut
from talkub.config.specs import (
    ARCHES,
    PLATFORMS,
    ROLES,
    ChartSpec,
    GitSpec,
    NodeSpec,
    SchematicSpec,
    TailscaleSpec,
    parse_installer_image,
)

__all__ = [
    "ARCHES",
    "DNS_LABEL",
    "MARKER",
    "PLATFORMS",
    "ROLES",
    "ChartSpec",
    "GitSpec",
    "NodeSpec",
    "Repo",
    "SchematicSpec",
    "Settings",
    "Shortcut",
    "TailscaleSpec",
    "find_repo",
    "parse_installer_image",
    "valid_label",
]
```

- [ ] **Step 4: Point importers at the package**

```bash
grep -rl "from talkub.config.repo import" src tests \
  | grep -v "src/talkub/config/" \
  | xargs sed -i '' 's/from talkub\.config\.repo import/from talkub.config import/'
uv run ruff check --fix --select I src tests && uv run ruff format src tests
```

If a test imported a private name (for example `_shortcut_from_dict`), import it from its new module under its new name (`from talkub.config.shortcuts import shortcut_from_dict`).

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_config_api.py tests/test_repo.py -v` → all pass.
Run: `uv run pytest -q` → 926 passed.

- [ ] **Step 6: Commit**

```bash
git add -A src tests
git commit -m "refactor: split config/repo.py into specs, shortcuts, settings and repo"
```

---

### Task 4: `kube/` and `talos/` packages (Tailscale host side goes to `talos/`)

**Files:**
- Move: `kube` → `kube/session`, `sealing` → `kube/sealing`; `nodes, lifecycle, upgrade, rotation, tailscale, factory, images` → `talos/`
- Modify: every importer (script), `src/talkub/kube/__init__.py`, `src/talkub/talos/__init__.py`, `src/talkub/talos/tailscale.py` (docstring)

**Interfaces:**
- Produces: `talkub.kube.session.{KubeSession, kube_session}`, `talkub.kube.sealing`, `talkub.talos.{nodes,lifecycle,upgrade,rotation,tailscale,factory,images}`

- [ ] **Step 1: Move the modules**

```bash
uv run python scripts/move_modules.py \
  kube=kube.session sealing=kube.sealing \
  nodes=talos.nodes lifecycle=talos.lifecycle upgrade=talos.upgrade rotation=talos.rotation \
  tailscale=talos.tailscale factory=talos.factory images=talos.images
uv run ruff check --fix --select I src tests scripts && uv run ruff format src tests scripts
```

Check: `grep -rnE "talkub\.(kube|sealing|nodes|lifecycle|upgrade|rotation|tailscale|factory|images)\b" src tests | grep -vE "talkub\.(kube\.(session|sealing)|talos\.)"` prints nothing. `grep -rn '"talkub.talos.rotation' tests/test_cli_rotate.py` shows the rewritten monkeypatch strings.

- [ ] **Step 2: Package docstrings**

- `src/talkub/kube/__init__.py`: `"""Kubernetes access: kubectl/helm sessions with a decrypted kubeconfig, and kubeseal."""`
- `src/talkub/talos/__init__.py` (currently empty): `"""Talos Linux: machine configs, node lifecycle, upgrades, CA rotation, installer images and the Tailscale system extension."""`

- [ ] **Step 3: Say which Tailscale this is**

`src/talkub/talos/tailscale.py` line 1: prepend `Tailscale on the Talos hosts (system extension, firewall, lockdown). The in-cluster operator is talkub.gitops.tailscale_operator.` as the first sentence of the existing module docstring and keep the rest.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest -q` → 926 passed.

- [ ] **Step 5: Commit**

```bash
git add -A src tests
git commit -m "refactor: group kube and talos modules; tailscale host side joins talos"
```

---

### Task 5: `argocd/`, `helm/`, `quality/` and `gitops/` packages

**Files:**
- Move: `apps` → `argocd/apps`, `argoapps` → `argocd/app_files`; `charts` → `helm/charts`; `lint, checks, hooks` → `quality/`; `bootstrap, chart_upgrade, gitrepo, coredns, tailscale_operator, renovate` → `gitops/`
- Modify: every importer (script), package `__init__.py` docstrings, `src/talkub/core/findings.py:1`, `src/talkub/gitops/tailscale_operator.py` (docstring)

**Interfaces:**
- Produces: `talkub.argocd.apps`, `talkub.argocd.app_files` (formerly `argoapps`: `HelmSource`, `helm_sources`, `app_source`, `set_target_revision`, `SECRET_TYPE_LABEL`), `talkub.helm.charts`, `talkub.quality.{lint,checks,hooks}`, `talkub.gitops.{bootstrap,chart_upgrade,gitrepo,coredns,tailscale_operator,renovate}`

- [ ] **Step 1: Move the modules**

```bash
uv run python scripts/move_modules.py \
  apps=argocd.apps argoapps=argocd.app_files charts=helm.charts \
  lint=quality.lint checks=quality.checks hooks=quality.hooks \
  bootstrap=gitops.bootstrap chart_upgrade=gitops.chart_upgrade gitrepo=gitops.gitrepo \
  coredns=gitops.coredns tailscale_operator=gitops.tailscale_operator renovate=gitops.renovate
uv run ruff check --fix --select I src tests scripts && uv run ruff format src tests scripts
```

The script renames attribute uses `argoapps.x` → `app_files.x` in files that did `from talkub import argoapps`. Check: `grep -rnw "argoapps" src tests` shows only docstrings/comments. Reword those to `app_files`.

- [ ] **Step 2: Package docstrings**

- `src/talkub/argocd/__init__.py`: `"""Argo CD Application files under gitops/apps/: rendering new ones, parsing and editing existing ones."""`
- `src/talkub/helm/__init__.py` stays as written in Task 1.
- `src/talkub/quality/__init__.py`: `"""Repo guard rails: lint (yamllint, kubeconform, helm template), pre-commit checks and the git hook."""`
- `src/talkub/gitops/__init__.py`: `"""Flows that change what Argo CD deploys: bootstrap, chart upgrades, the git repo, CoreDNS, the Tailscale operator, Renovate."""`

- [ ] **Step 3: Fix docstrings that name old modules**

- `src/talkub/core/findings.py:1`: `` `talkub.lint` and `talkub.argoapps` `` → `` `talkub.quality.lint` and `talkub.argocd.app_files` ``.
- `src/talkub/gitops/tailscale_operator.py` line 1: prepend `The Tailscale Kubernetes operator (in-cluster). Tailscale on the hosts is talkub.talos.tailscale.` as the first sentence of the existing docstring.
- `grep -rnE "talkub\.(apps|argoapps|charts|lint|checks|hooks|bootstrap|chart_upgrade|gitrepo|coredns|tailscale_operator|renovate)\b" src tests` prints nothing.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest -q` → 926 passed.

- [ ] **Step 5: Commit**

```bash
git add -A src tests
git commit -m "refactor: group argocd, helm, quality and gitops modules"
```

---

### Task 6: `workstation/` and `overview/` packages

**Files:**
- Move: `access, shortcuts, clipboard, clipboard_clear` → `workstation/`; `status, versions` → `overview/`
- Modify: `src/talkub/workstation/clipboard.py:87` (rewritten by the script), `src/talkub/workstation/clipboard_clear.py:1` (docstring), `tests/test_clipboard.py:141` (rewritten by the script)

**Interfaces:**
- Produces: `talkub.workstation.{access,shortcuts,clipboard,clipboard_clear}`, `talkub.overview.{status,versions}`

- [ ] **Step 1: Move the modules**

```bash
uv run python scripts/move_modules.py \
  access=workstation.access shortcuts=workstation.shortcuts \
  clipboard=workstation.clipboard clipboard_clear=workstation.clipboard_clear \
  status=overview.status versions=overview.versions
uv run ruff check --fix --select I src tests scripts && uv run ruff format src tests scripts
```

- [ ] **Step 2: Check the `-m` entry point**

`grep -n "talkub.workstation.clipboard_clear" src/talkub/workstation/clipboard.py tests/test_clipboard.py` shows the argv string in `clipboard.py` and the assertion in the test. Update the docstring in `clipboard_clear.py:1` to `` `python -m talkub.workstation.clipboard_clear` ``.

Run: `uv run python -c "import importlib.util as u; assert u.find_spec('talkub.workstation.clipboard_clear')"` → no output. (Do not run the helper itself: it sleeps 45 s and touches the real clipboard.)

- [ ] **Step 3: Package docstrings**

- `src/talkub/workstation/__init__.py`: `"""The admin's machine: shells with decrypted credentials, shortcuts, the clipboard."""`
- `src/talkub/overview/__init__.py`: `"""Read-only views across the whole cluster: status and versions."""`

- [ ] **Step 4: Nothing but wiring left at the top level**

Run: `ls src/talkub/*.py` → exactly `__init__.py`, `context.py`, `scaffold.py`.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest -q` → 926 passed.

- [ ] **Step 6: Commit**

```bash
git add -A src tests
git commit -m "refactor: group workstation and overview modules"
```

---

### Task 7: Enforce the layers with import-linter

**Files:**
- Modify: `pyproject.toml`, `uv.lock`

- [ ] **Step 1: Add the dependency and the contract**

```bash
uv add --dev import-linter
```

Append to `pyproject.toml`:

```toml
[tool.importlinter]
root_package = "talkub"

# Higher layers may import lower ones, never the reverse; `|` separates independent siblings.
# See docs/superpowers/specs/2026-09-26-talkub-package-structure-design.md.
[[tool.importlinter.contracts]]
name = "Domain layers"
type = "layers"
containers = ["talkub"]
layers = [
    "cli",
    "scaffold | overview",
    "gitops | workstation",
    "quality",
    "helm | talos",
    "argocd",
    "kube",
    "context",
    "secrets",
    "config",
    "core",
]
```

- [ ] **Step 2: Run it**

Run: `uv run lint-imports`
Expected: `Domain layers KEPT`, `Contracts: 1 kept, 0 broken.`

- [ ] **Step 3: Prove it catches a violation**

Temporarily add `from talkub.gitops import bootstrap  # noqa: F401` to `src/talkub/quality/checks.py`.
Run: `uv run lint-imports` → `BROKEN`, naming `talkub.quality.checks -> talkub.gitops.bootstrap`.
Remove the line again, then run `uv run lint-imports` → KEPT.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build: enforce package layers with import-linter"
```

---

### Task 8: Mirror the packages in `tests/`

**Files:**
- Move (all with `git mv`), relative to `tests/`:

| Target dir | Test files |
|---|---|
| `core/` | `test_ephemeral.py`, `test_fsutil.py`, `test_runner.py` |
| `config/` | `test_repo.py`, `test_schematic.py`, `test_config_api.py` |
| `secrets/` | `test_age.py`, `test_sops.py`, `test_sops_edit.py`, `test_identity.py`, `test_keyring.py`, `test_recipients.py` |
| `kube/` | `test_kube.py`, `test_sealing.py` |
| `talos/` | `test_client.py`, `test_inspect.py`, `test_render.py`, `test_talos_gen.py`, `test_nodes.py`, `test_lifecycle.py`, `test_upgrade.py`, `test_rotate.py`, `test_rotation_ca.py`, `test_tailscale.py`, `test_tailscale_flows.py`, `test_factory.py`, `test_images.py` |
| `argocd/` | `test_apps.py`, `test_argoapps.py` → rename to `test_app_files.py` |
| `helm/` | `test_charts.py`, `test_helm.py` |
| `quality/` | `test_lint.py`, `test_checks.py`, `test_hooks.py` |
| `gitops/` | `test_bootstrap.py`, `test_chart_upgrade.py`, `test_gitrepo.py`, `test_gitrepo_credentials.py`, `test_gitrepo_flows.py`, `test_coredns.py`, `test_coredns_takeover.py`, `test_tailscale_operator.py`, `test_renovate.py` |
| `workstation/` | `test_access.py`, `test_clipboard.py`, `test_shortcuts.py` |
| `overview/` | `test_status.py`, `test_versions.py` |
| `cli/` | all `test_cli_*.py` |
| stays in `tests/` | `conftest.py`, `helpers.py`, `test_scaffold.py` |

`tests/talos/test_tailscale.py` (host) and `tests/gitops/test_tailscale_operator.py` have different basenames, so nothing collides.

- [ ] **Step 1: Move the files**

```bash
cd tests
for spec in \
  "core:test_ephemeral test_fsutil test_runner" \
  "config:test_repo test_schematic test_config_api" \
  "secrets:test_age test_sops test_sops_edit test_identity test_keyring test_recipients" \
  "kube:test_kube test_sealing" \
  "talos:test_client test_inspect test_render test_talos_gen test_nodes test_lifecycle test_upgrade test_rotate test_rotation_ca test_tailscale test_tailscale_flows test_factory test_images" \
  "argocd:test_apps" \
  "helm:test_charts test_helm" \
  "quality:test_lint test_checks test_hooks" \
  "gitops:test_bootstrap test_chart_upgrade test_gitrepo test_gitrepo_credentials test_gitrepo_flows test_coredns test_coredns_takeover test_tailscale_operator test_renovate" \
  "workstation:test_access test_clipboard test_shortcuts" \
  "overview:test_status test_versions"; do
  dir=${spec%%:*}; mkdir -p "$dir"
  for name in ${spec#*:}; do git mv "$name.py" "$dir/"; done
done
git mv test_argoapps.py argocd/test_app_files.py
mkdir -p cli && git mv test_cli_*.py cli/
cd ..
```

- [ ] **Step 2: Check that nothing is left behind and names are unique**

Run: `ls tests/*.py` → `conftest.py`, `helpers.py`, `test_scaffold.py`.
Run: `find tests -name "test_*.py" -exec basename {} \; | sort | uniq -d` → no output.

- [ ] **Step 3: Run the tests**

`conftest.py` at `tests/` applies to all subdirectories, and `pythonpath = ["tests"]` keeps `from helpers import ...` working.
Run: `uv run pytest -q` → 926 passed (same count as before the move: no test was lost).

- [ ] **Step 4: Commit**

```bash
git add -A tests
git commit -m "test: mirror the source packages in tests/"
```

---

### Task 9: Document the structure, drop the move script, smoke-test the wheel

**Files:**
- Modify: `README.md` (section `## Development`, currently line 343)
- Delete: `scripts/move_modules.py` (and `scripts/` if empty)

- [ ] **Step 1: Write the architecture section**

Replace the `## Development` section of `README.md` with:

````markdown
## Development

    uv run pytest
    uv run ruff check src tests
    uv run lint-imports

### Code layout

`src/talkub/` is split by domain. `cli/` holds one thin module per command group and calls into
the packages below it. Each package's `__init__.py` says what it covers.

| Package | What lives there |
|---|---|
| `cli/` | Typer commands, argument parsing, prompts, console output |
| `scaffold.py` | `talkub init`: writes a new cluster repo from `templates/` |
| `overview/` | `status`, `versions`: read-only views across the cluster |
| `gitops/` | Flows that change what Argo CD deploys: bootstrap, chart upgrade, git repo, CoreDNS, Tailscale operator, Renovate |
| `workstation/` | The admin's machine: credential shells, shortcuts, clipboard |
| `quality/` | `lint`, `check`, pre-commit hook |
| `helm/` | Chart catalog and `helm` command lines |
| `talos/` | Machine configs, node lifecycle, upgrades, CA rotation, installer images, Tailscale on the hosts |
| `argocd/` | Rendering, parsing and editing `gitops/apps/*.yaml` |
| `kube/` | kubectl/helm sessions, kubeseal |
| `context.py` | `Context`: repo + runner + keys, passed to every flow |
| `secrets/` | age identities, recipients, master key, SOPS |
| `config/` | The repo on disk and `.talkub.yaml` (`Repo`, `Settings`, specs) |
| `core/` | Errors, running tools, YAML, atomic writes, temp files |

The table is ordered top to bottom by layer: a package may import the ones below it, never the
ones above. `uv run lint-imports` enforces this (contract in `pyproject.toml`).

Adding a command: put the logic into the package that owns the domain, add a thin module in
`cli/`, register it in `cli/main.py`, and put the tests into the matching folder under `tests/`.
````

- [ ] **Step 2: Remove the move script**

```bash
git rm scripts/move_modules.py
rmdir scripts 2>/dev/null || true
```

- [ ] **Step 3: Smoke-test the built wheel**

```bash
uv build --wheel -o /tmp/talkub-wheel
unzip -l /tmp/talkub-wheel/talkub-*.whl | grep -E "talkub/(core|config|secrets|kube|talos|argocd|helm|quality|gitops|workstation|overview)/__init__.py" | wc -l
```

Expected: `11`.

```bash
uv run --isolated --with /tmp/talkub-wheel/talkub-*.whl talkub --help
```

Expected: the command list, no traceback.

- [ ] **Step 4: Final verification**

Run: `uv run pytest -q` → 926 passed.
Run: `uv run lint-imports` → KEPT.
Run: `uv run ruff check src tests --statistics` → only the 30 pre-existing findings.

- [ ] **Step 5: Commit**

```bash
git add -A README.md scripts
git commit -m "docs: describe the package layout; drop the one-off move script"
```
