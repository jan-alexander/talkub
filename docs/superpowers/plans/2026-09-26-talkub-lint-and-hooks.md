# talkub lint and pre-commit hook Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `talkub lint [--offline]` (yamllint, kubeconform on gitops manifests, Helm-rendered Argo CD apps through kubeconform) and `talkub hooks install/uninstall` (pre-commit hook running `talkub check && talkub lint --offline`), wired into `init`.

**Architecture:** `src/talkub/lint.py` holds the report model and the three checks; `src/talkub/hooks.py` renders/installs the hook; thin CLI modules `cli/lint.py`, `cli/hooks.py`; `scaffold.py`/`cli/setup.py` write `.yamllint` and install the hook on `init`.

**Tech Stack:** Python ≥ 3.12, Typer, ruamel.yaml, yamllint (new dependency), pytest; kubeconform, helm, git.

**Spec:** `docs/superpowers/specs/2026-09-26-talkub-lint-and-hooks-design.md`

## Global Constraints

- English code, comments, CLI output. `runner.py` is the only subprocess starter; argv lists only. yamllint is used as a library (no subprocess).
- Finding format: `<path relative to repo root>:<line>: <error|warning>: <text>` (line omitted when unknown); summary line `lint: <E> errors, <W> warnings, <S> skipped`; exit 1 iff E > 0.
- `DEFAULT_YAMLLINT` exactly:
```
# Managed by talkub. yamllint configuration for this cluster repo.
extends: relaxed
ignore: |
  *.sops.yaml
rules:
  line-length: disable
```
- kubeconform schema location for CRDs exactly: `https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json`; cache dir `$XDG_CACHE_HOME/talkub/kubeconform` (default `~/.cache/talkub/kubeconform`).
- Hook marker line text: `talkub pre-commit hook`; hook body per spec §3.
- Commits end with `Co-Authored-By: <model> <noreply@anthropic.com>`. `uv run pytest`; tests needing kubeconform/helm/network use `need(...)` and skip on network failure.

## Review Focus

1. **A fresh `init` repo must pass `lint`** (templates + generated files vs. DEFAULT_YAMLLINT) — test in Task 1.
2. **Missing tools never fail the commit hook** (kubeconform/helm absent → skipped warnings, exit 0 if nothing else) — tests in Tasks 2/3.
3. **Offline mode never needs the network** (empty cache → skipped) — test in Task 2.
4. **Foreign pre-commit hook is never overwritten without `--force`** — test in Task 4.
5. **The hook actually blocks a commit** on `talkub check` failure — test in Task 4.

---

### Task 1: Report model, yamllint check, `talkub lint`

**Files:** Modify `pyproject.toml` (`yamllint` dependency, `uv lock`), Create `src/talkub/lint.py`, `src/talkub/cli/lint.py`; Modify `src/talkub/cli/main.py`; Test `tests/test_lint.py`.

**Interfaces (produced):**
```python
@dataclass(frozen=True)
class Finding:
    level: str            # "error" | "warning"
    path: str             # repo-relative, or an app name prefix like "app coredns"
    message: str
    line: int | None = None

@dataclass
class LintReport:
    findings: list[Finding] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)   # human-readable reasons
    @property
    def errors(self) -> int: ...
    @property
    def warnings(self) -> int: ...
    def render(self) -> list[str]   # finding lines, then "skipped: <reason>" lines, then the summary line

DEFAULT_YAMLLINT: str   # exact text from Global Constraints
def yaml_files(repo: Repo) -> list[Path]           # *.yaml/*.yml under root, excluding .git/, sorted
def lint_yaml(repo: Repo, report: LintReport) -> None
def run(ctx: Context, *, offline: bool) -> LintReport   # Task 1: only lint_yaml; Tasks 2-3 extend
```
- `lint_yaml`: config from `<root>/.yamllint` if present (`YamlLintConfig(content=...)`), else `DEFAULT_YAMLLINT`; skip files matched by `conf.is_file_ignored(relpath)`; `yamllint.linter.run(text, conf, relpath)` → Finding(level, relpath, desc + " (" + rule + ")" when rule, line).
- CLI `talkub lint [--offline]`: prints `report.render()`, `raise typer.Exit(1 if report.errors else 0)`. `lint` needs no master key (do not open the keyring).

- [ ] Tests first: error and warning findings with lines; `*.sops.yaml` ignored by default; a repo `.yamllint` overrides the default (e.g. enabling line-length makes a long line an error); `yaml_files` excludes `.git`; summary/exit code via CLI; **a fresh `init` repo (use the `init_cluster` path from tests/test_scaffold.py with fake helm) has zero yamllint errors** — fix generated files/templates if needed (e.g. missing newline at EOF, trailing spaces) rather than weakening DEFAULT_YAMLLINT.
- [ ] Implement, PASS, full suite, commit `feat: talkub lint with yamllint`.

---

### Task 2: kubeconform on gitops manifests

**Files:** Modify `src/talkub/lint.py`, `src/talkub/runner.py` (`INSTALL_HINTS["kubeconform"] = "brew install kubeconform"`); Test `tests/test_lint.py`.

**Interfaces:** `manifest_files(repo) -> list[Path]` (gitops/apps/** and gitops/manifests/** `*.y*ml`, excluding `*.sops.yaml`); `cache_dir() -> Path`; `kubeconform_argv(version: str, *, offline: bool) -> list[str]` (without inputs); `parse_kubeconform(stdout: str, prefix: str | None = None) -> tuple[list[Finding], list[str]]` (findings, skipped reasons) for `-output json`; `lint_manifests(ctx, report, *, offline)`.
- argv: `["kubeconform", "-output", "json", "-summary", "-strict", "-ignore-missing-schemas", "-kubernetes-version", ver, "-cache", str(cache), "-schema-location", "default", "-schema-location", DATREE]`.
- JSON: kubeconform `-output json` prints `{"resources": [{"filename","kind","name","version","status","msg"}...], "summary": {...}}`. status `statusInvalid`/`statusError` → error finding (path = filename relative to repo, message = `<kind>/<name>: <msg>`); `statusSkipped` → skipped reason `no schema for <kind> (<version>)`; valid → nothing. (Verify the exact JSON shape with the real tool — `need("kubeconform")` test.)
- Version: `settings.kubernetes_version.lstrip("v")`; if the run shows errors for core kinds indicating the schema version is missing (msg contains "could not find schema" / 404 for kinds without a group), rerun with `master` and add a warning finding `kubernetes schemas for <ver> not available; validated against master`.
- Offline: cache dir missing/empty → skip whole check with reason `kubeconform: no schema cache yet (run \`talkub lint\` online once)`; otherwise run normally (the cache serves), and download errors become skipped reasons.
- kubeconform missing (`shutil.which`) → skip reason `kubeconform not found (brew install kubeconform)`; the Helm check (Task 3) will also skip.
- Wire into `run()` after `lint_yaml`.
- [ ] Tests: spy kubeconform returning crafted JSON → findings/skipped; argv contents; manifest file selection (values/ and sops excluded); missing tool; offline with empty cache → no kubeconform call; version fallback rerun; real-tool test (need kubeconform, skip on network error) on a small valid Deployment + an invalid one.
- [ ] Implement, PASS, full suite, commit `feat: lint gitops manifests with kubeconform`.

---

### Task 3: Helm-rendered Argo CD apps

**Files:** Modify `src/talkub/lint.py`; Test `tests/test_lint.py`.

**Interfaces:** `@dataclass(frozen=True) HelmSource(app: str, release: str, namespace: str, repo: str, chart: str, version: str, value_files: tuple[Path, ...], inline_values: str | None)`; `helm_sources(repo: Repo) -> tuple[list[HelmSource], list[Finding]]` (parse gitops/apps/*.yaml; findings for missing `$values/` files); `lint_helm_apps(ctx, report, *, offline)`.
- For each Application (skip non-Application docs), each source with `chart`: `repoURL`, `targetRevision`, `spec.destination.namespace` (default `default`), release = `helm.releaseName` or `metadata.name`; `helm.valueFiles` entries starting with `$values/` → `repo.root / rest` (must exist, else error finding `app <name>: values file <path> not found`); other valueFiles → warning (unsupported, skipped); `helm.values` (string) or `helm.valuesObject` (mapping → yamlio.dump) → inline values written to a file in `private_tempdir()`.
- Render: `helm template <release> <ref…> --version <v> -n <ns> [-f …]` via `Runner.run(check=False)`; ref from `bootstrap.chart_ref(ChartSpec(repo, chart, version))`; non-zero → error finding `app <name>: helm template failed: <first 5 stderr lines>`.
- Validate: `kubeconform <argv> -` with the rendered manifest on stdin (`Runner.run(input=...)`), findings prefixed `app <name>`.
- Skips: `offline` → skip reason `helm apps: skipped (--offline)`; helm missing → `helm not found`; kubeconform missing → already skipped (reason not duplicated).
- [ ] Tests: `helm_sources` on the real app templates (argocd, sealed-secrets, coredns via renderers) incl. $values mapping, releaseName, namespace, inline values/valuesObject, missing values file; spy helm + spy kubeconform: argv, stdin passthrough, failure paths; offline skip; real test (need helm + kubeconform, network skip) rendering the coredns app from `coredns.values()`.
- [ ] Implement, PASS, full suite, commit `feat: lint Helm-rendered Argo CD apps`.

---

### Task 4: Pre-commit hook and `init` integration

**Files:** Create `src/talkub/hooks.py`, `src/talkub/cli/hooks.py`; Modify `src/talkub/cli/main.py`, `src/talkub/scaffold.py`, `src/talkub/cli/setup.py`, `README.md`; Test `tests/test_hooks.py`, `tests/test_scaffold.py`/`tests/test_cli_manage.py`.

**Interfaces:** `MARKER = "talkub pre-commit hook"`; `talkub_path() -> str`; `render_hook(talkub: str) -> str`; `hook_path(repo, runner) -> Path` (`git rev-parse --git-dir` with `cwd=repo.root`, relative → joined to root; not a git repo → `ValidationError(..., hint="git init")`); `install(repo, runner, *, force=False) -> list[str]` (notes); `uninstall(repo, runner) -> Path`.
- Hook body exactly per spec §3 with `shlex.quote` for the path; file mode 0755; `hooks/` dir created if missing.
- `install`: existing file with MARKER → overwrite; existing without → `ValidationError("a pre-commit hook already exists", hint="talkub hooks install --force")` unless force; write `.yamllint` (lint.DEFAULT_YAMLLINT) if missing and add note `wrote .yamllint — commit it`.
- `uninstall`: only a file with MARKER, else error.
- `talkub_path()`: `shutil.which(sys.argv[0])` → resolved absolute path if found; else `Path(sys.argv[0]).resolve()` if it exists and is executable; else `"talkub"`.
- `scaffold.init_cluster`: write `.yamllint` (DEFAULT_YAMLLINT) with the other files; `cli/setup.init`: after `git_init`, `hooks.install(repo, Runner())` and print notes; errors become warnings.
- CLI `talkub hooks install [--force]`, `talkub hooks uninstall`.
- README section "Lint and pre-commit hook": `talkub lint` (checks, `--offline`, needed tools), `talkub hooks install`, bypass with `git commit --no-verify`.
- [ ] Tests: install into a real `git init` repo (content, mode, quoted path), marker overwrite, foreign hook with/without force, uninstall own/foreign/missing, not a git repo; **real commit test**: git repo, hook installed pointing at a fake talkub script (exit 1 for `check`) → `git commit` fails; with exit 0 → succeeds (set user.name/email via `-c`); init writes `.yamllint` and installs the hook (existing init tests keep passing).
- [ ] Implement, PASS, full suite, commit `feat: pre-commit hook and lint on init`.
