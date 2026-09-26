# talkub versions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Argo CD Application files become the only source of chart repo/chart/version (`.talkub.yaml` loses `charts`); `talkub versions` shows target/running/latest for Talos, Kubernetes and chart apps; `talkub versions set` and `talkub chart upgrade` update versions; `talkub renovate init` writes a Renovate config.

**Architecture:** New `src/talkub/argoapps.py` (parser moved from lint + `app_source`, `set_target_revision`); `charts.CATALOG` for creating new apps only; `bootstrap.helm_install` takes a `HelmSource`; new modules `versions.py`, `chart_upgrade.py`, `renovate.py` with thin CLI modules.

**Tech Stack:** Python ≥ 3.12, Typer, ruamel.yaml, pytest; helm, kubectl, talosctl, kubeconform; `urllib.request` for GitHub / dl.k8s.io.

**Spec:** `docs/superpowers/specs/2026-09-26-talkub-versions-design.md`

## Global Constraints

- English code, comments, CLI output. `runner.py` is the only subprocess starter; HTTP via `urllib.request` with a timeout (10 s), User-Agent `talkub`.
- `.talkub.yaml` never contains `charts` after any write; a legacy `charts` block is ignored on load (no error, no warning).
- App file for chart key K: `gitops/apps/K.yaml`; CATALOG keys: `argocd`, `sealed-secrets`, `coredns`, `tailscale-operator` (repo/chart pairs exactly as in the current `DEFAULT_CHARTS`/`OPTIONAL_CHARTS`).
- Bootstrap order: `coredns` (only if its app file exists), `sealed-secrets`, `argocd`; `argocd` with `--create-namespace`, others without; namespace/release/values from the app file.
- Latest Talos: `https://api.github.com/repos/siderolabs/talos/releases/latest` → `tag_name`. Latest Kubernetes: `https://dl.k8s.io/release/stable.txt` (strip `v`). Tests use a local HTTP stub; base URLs overridable via env `TALKUB_GITHUB_API` and `TALKUB_K8S_RELEASE_URL`.
- Renovate JSON exactly per spec §5 (verify `managerFilePatterns` vs `fileMatch` against the current Renovate docs and note the result in the commit message).
- Commits end with `Co-Authored-By: <model> <noreply@anthropic.com>`. `uv run pytest`.

## Review Focus

1. **Existing repos keep working** after `charts` disappears (homelab_2 has a `charts` block and all app files) — tests in Task 2.
2. **bootstrap installs exactly what the app files say** (version, values files, namespace, release) — test in Task 2.
3. **`versions` never aborts** on network/cluster errors — test in Task 3.
4. **`chart upgrade` changes only `targetRevision` of the chart source** (comments, git source untouched) — test in Tasks 1/4.
5. **`chart upgrade --all` continues after one app fails** and exits 1 at the end — test in Task 4.

---

### Task 1: `argoapps.py`

**Files:** Create `src/talkub/argoapps.py`; Modify `src/talkub/lint.py` (import from argoapps, remove moved code); Move tests from `tests/test_lint.py` to `tests/test_argoapps.py` where they only test parsing.

**Interfaces:**
- `HelmSource` and `helm_sources(repo) -> tuple[list[HelmSource], list[Finding]]` moved unchanged (keep `Finding` in lint; argoapps may import it from lint only if no cycle — otherwise move `Finding` to a small `talkub/findings.py` and re-export from lint).
- `app_source(repo, key: str) -> HelmSource`: `gitops/apps/<key>.yaml` must exist and contain exactly one Application with exactly one chart source → else `ValidationError(f"gitops/apps/{key}.yaml has no Helm chart source", hint=...)`; `$values/` value-file findings (missing file) become a `ValidationError` here.
- `set_target_revision(repo, app: str, version: str) -> bool`: round-trip load `gitops/apps/<app>.yaml`, set `targetRevision` of the (single) chart source, write only if changed; returns changed. Comments and other sources unchanged.
- [ ] Tests: moved helm_sources tests pass; app_source ok/missing file/no chart source/two chart sources; set_target_revision keeps comments byte-identical except the version and leaves the `ref: values` source's `targetRevision: HEAD` alone.
- [ ] Commit `refactor: argoapps parser shared by lint and friends`.

### Task 2: Drop `charts` from settings; app files are the source

**Files:** Modify `src/talkub/repo.py`, `src/talkub/charts.py`, `src/talkub/bootstrap.py`, `src/talkub/coredns.py`, `src/talkub/tailscale_operator.py`, `src/talkub/scaffold.py`, `src/talkub/cli/repo.py` (if it touches charts), README; Tests across `tests/test_repo.py`, `tests/test_bootstrap.py`, `tests/test_coredns*.py`, `tests/test_tailscale_operator.py`, `tests/test_scaffold.py`, `tests/helpers.py`.

- `Settings.charts` removed (and `ChartSpec` only kept if still used by charts.py/renderers); `from_dict` ignores `charts`; `to_dict` never writes it.
- `charts.CATALOG: dict[str, tuple[str, str]]` (merge of DEFAULT_CHARTS + OPTIONAL_CHARTS); `resolve(runner, key) -> ChartSpec` unchanged semantics; `resolve_defaults` → returns the argocd + sealed-secrets specs for init.
- `bootstrap.helm_install(kube, source: HelmSource, *, create_namespace: bool)` uses `source.release`, `chart_ref(ChartSpec(source.repo, source.chart, source.version))`, `--version source.version`, `-n source.namespace`, `-f` each value file, inline values via private temp file (reuse lint's approach), `--wait --timeout 10m`.
- `bootstrap.run`: order per Global Constraints via `argoapps.app_source`; missing sealed-secrets/argocd app → `ValidationError`. `upgrade_argocd` uses `app_source(repo, "argocd")`.
- `scaffold`: template values from the resolved specs (not settings); no `charts` in written settings.
- `coredns`: resume check uses app/values files only; takeover writes the app with `charts.resolve`, no settings.charts; Helm install from `app_source`.
- `tailscale_operator.enable`: no settings.charts write.
- [ ] Tests: legacy `charts` ignored and not written back; bootstrap argv from app files (version/values/namespace/release) incl. coredns first; missing app error; init output has no `charts`; takeover/operator enable don't write `charts`; full suite green. README: remove mentions of charts in `.talkub.yaml`.
- [ ] Commit `refactor: Argo CD apps are the single source of chart versions`.

### Task 3: `talkub versions` and `versions set`

**Files:** Create `src/talkub/versions.py`, `src/talkub/cli/versions.py`; register in `cli/main.py`; Test `tests/test_versions.py`.

- `Row(component, target, running, latest, note)`; `collect(ctx, *, offline) -> list[Row]`; `render(rows) -> str` (aligned columns, `*` prefix when latest not in {"?", "-"} and latest != target).
- Talos running: reuse the talosctl version query used by `status` (factor a helper if needed); needs talosconfig — any error → `?`. Kubernetes running: kubelet versions like `status` (kubectl get nodes) — any error → `?`. Multiple distinct values comma-joined.
- Chart rows: every app with a chart source (`argoapps.helm_sources`); running from `kubectl -n argocd get application <name> -o json` — for multi-source apps take `status.sync.revisions[i]` where i is the chart source index, else `status.sync.revision`; latest via `charts.latest_version`; note `appVersion a → b` via `helm show chart` of both versions (errors → no note).
- HTTP helper `_get_text(url) -> str | None` (errors → None).
- `set_version(repo, component, version)`: talos requires `v\d+\.\d+\.\d+`; kubernetes accepts `v?\d+\.\d+\.\d+` and stores without `v`; writes settings; returns the follow-up hint.
- CLI: `talkub versions [--offline]` (callback, invoke_without_command) and `talkub versions set (talos|kubernetes) VERSION`.
- [ ] Tests with spies + HTTP stub (see factory_stub fixture pattern): all rows, `?` on failures, `--offline` → `-`, `*` marking, set validation/writes.
- [ ] Commit `feat: talkub versions`.

### Task 4: `talkub chart upgrade`

**Files:** Create `src/talkub/chart_upgrade.py`, `src/talkub/cli/chart.py`; register; Test `tests/test_chart_upgrade.py`.

- `upgrade(ctx, app: str | None, *, all_apps: bool, version: str | None, confirm) -> int` per spec §4: target = version or latest; same → "already at X"; show `chart: old → new`, `appVersion`, unified diff of `helm show values` old vs new (max 200 lines + "… (N more lines)"); confirm (default yes); `argoapps.set_target_revision`; lint only this app (factor `lint.lint_helm_app(ctx, report, source, offline=False)` out of `lint_helm_apps`); errors → exit 1 with hint "fix the values or revert the change"; note commit/push, and for `argocd` "Argo CD upgrades itself".
- `--all`: iterate all chart apps; continue after failures; summary; exit 1 if any failed. `--version` with `--all` → error.
- [ ] Tests with spy helm/kubeconform: happy path (file changed, diff shown), already current, decline, lint failure, `--all` with one failure, invalid flag combos.
- [ ] Commit `feat: talkub chart upgrade`.

### Task 5: `talkub renovate init` and docs

**Files:** Create `src/talkub/renovate.py`, `src/talkub/cli/renovate.py`; register; README section "Versions and updates"; Test `tests/test_renovate.py`.
- `render() -> str` (JSON per spec §5, 2-space indent, trailing newline); `init(repo, *, force) -> Path`; notes: enable https://github.com/apps/renovate for the repo; chart PRs are rolled out by Argo CD on merge → run `talkub lint` on the PR branch first; Talos/Kubernetes PRs need `talkub upgrade` after merge.
- README: single source (app files), `talkub versions`, `versions set`, `chart upgrade`, `renovate init`.
- [ ] Tests: JSON parses and contains the managers/rules; existing file without/with `--force`.
- [ ] Commit `feat: talkub renovate init`.
