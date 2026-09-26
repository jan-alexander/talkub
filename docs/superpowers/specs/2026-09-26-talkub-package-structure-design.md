# talkub package structure — design

## Problem

`src/talkub/` has ~50 modules at the top level. Domains (secrets, Talos nodes, Argo CD, Helm,
Tailscale, lint) are only visible from file names, several names are easy to confuse
(`repo`/`gitrepo`, `apps`/`argoapps`, `nodes`/`talos/`, `tailscale`/`tailscale_operator`), and a
few imports point from lower-level code into orchestration flows:

- `lint`, `chart_upgrade` and `coredns` import `bootstrap.chart_ref` / `bootstrap.helm_install`.
- `checks` imports `SECRET_TYPE_LABEL` from `gitrepo`.
- `cli/hooks` and `cli/setup` import `print_notes` from `cli/tailscale`.
- `repo.py` (590 lines) mixes every spec type, shortcut parsing, `Settings` and `Repo`.

Goal: a structure where a new developer sees the domains in the directory listing, finds the code
behind a command quickly, and where the dependency direction is written down and enforced.

## Decisions (agreed 2026-09-26)

- Package by domain, **technical package names** (`talos`, `kube`, `helm`, `argocd`, ...).
- **Tailscale is split**: the host side (Talos system extension, firewall, lockdown, config
  rewrites) belongs to `talos/`; the in-cluster operator belongs to `gitops/`.
- `cli/` stays flat: one module per command group. Shared CLI helpers live in `cli/output.py`,
  `cli/prompts.py`, `cli/state.py`.
- Module file names stay as they are unless the package does not already remove the confusion.
  The only rename is `argoapps` → `argocd/app_files` (and `kube` → `kube/session`).

## Target layout

```
talkub/
├── core/          errors, runner, fsutil, yamlio, ephemeral, findings
├── config/        repo (Repo, find_repo, MARKER), settings (Settings), specs (NodeSpec, ChartSpec,
│                  SchematicSpec, TailscaleSpec, GitSpec, ...), shortcuts (Shortcut + parsing),
│                  schematic (Image Factory customization hash, pure)
├── secrets/       age, sops, identity, keyring, recipients
├── context.py     Context: repo + runner + keyring + sops, passed to every flow
├── kube/          session (kube_session), sealing (kubeseal)
├── talos/         client, inspect, render, gen, nodes, lifecycle, upgrade, rotation,
│                  tailscale (host side), factory, images
├── argocd/        apps (Application renderers), app_files (parse/edit gitops/apps/*.yaml,
│                  SECRET_TYPE_LABEL)
├── helm/          charts (catalog, show/latest), install (chart_ref, helm_install)
├── quality/       lint, checks, hooks
├── gitops/        bootstrap, chart_upgrade, gitrepo, coredns, tailscale_operator, renovate
├── workstation/   access, shortcuts, clipboard, clipboard_clear
├── overview/      status, versions
├── scaffold.py    `talkub init` (templates/ stays next to it)
└── cli/
```

## Dependency rule

Higher layers may import lower ones, never the reverse. Packages on the same line are
independent of each other.

```
talkub.cli
talkub.scaffold | talkub.overview
talkub.gitops | talkub.workstation
talkub.quality
talkub.helm | talkub.talos
talkub.argocd
talkub.kube
talkub.context
talkub.secrets
talkub.config
talkub.core
```

Enforced with import-linter (`lint-imports`), configured in `pyproject.toml`.

Why `quality` sits below `gitops`: `chart_upgrade` edits an app and then lints it, so it needs
`quality.lint`; `lint` in turn only needs the Argo CD file parser and `helm.chart_ref`, which live
in `argocd`/`helm` below it. `helm` sits above `argocd` because `helm_install` takes an Argo CD
app's `HelmSource`.

## Tests

`tests/` mirrors the packages (`tests/talos/test_lifecycle.py`, `tests/cli/test_cli_nodes.py`, ...).
Test file basenames stay unique, so pytest's default import mode keeps working without
`__init__.py` files. `conftest.py` and `helpers.py` stay at `tests/`.

## Non-goals

- No behaviour changes, no CLI changes.
- No renaming of public functions beyond what the moves require.
- The existing ruff findings (B008, PLW1510, SIM117) are out of scope.
