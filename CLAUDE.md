# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

talkub is an admin CLI (Python 3.12+, Typer, ruamel.yaml, managed with uv) for a *separate* Talos Linux
cluster repo: Argo CD app-of-apps, SOPS/age for infrastructure secrets, Sealed Secrets for app secrets.
This repo holds only the tool; the cluster repo it manages is marked by `.talkub.yaml` and is scaffolded
from `src/talkub/templates/repo/`. User-facing behavior of every command is documented in `README.md` —
keep it in sync when behavior changes.

## Commands

    uv run pytest                                   # all tests
    uv run pytest tests/talos/test_render.py        # one file
    uv run pytest tests/talos/test_render.py -k name  # one test
    uv run ruff check src tests                     # lint (add --fix to apply)
    uv run ruff format src tests                    # format (line length 120)
    uv run lint-imports                             # enforce the package layer contract
    uv run basedpyright                             # type check (not part of the pre-commit hook)
    uv run talkub --help                            # run the CLI from the working tree

Pre-commit hook (lint-imports, ruff check, ruff format --check), once per clone:
`git config core.hooksPath .githooks`.

## Architecture

**Layers.** `src/talkub/` is split by domain, and packages are strictly layered (enforced by
import-linter, contract in `pyproject.toml`): a package may import those below it, never above.

    cli → scaffold | overview → gitops | workstation → quality → helm | talos
        → argocd → kube → context → secrets → config → core

`|` marks independent siblings that must not import each other. Each package's `__init__.py`
describes its scope. The design rationale is in `docs/superpowers/specs/2026-09-26-talkub-package-structure-design.md`.

**Adding a command:** logic goes into the package that owns the domain; `cli/` gets a thin module
(argument parsing, prompts, output only) registered in `cli/main.py`; tests go into the matching
folder under `tests/`.

**Entry point (`cli/main.py`).** `main()` pre-parses `--repo`/`--debug`, then handles `t`/`talosctl`,
`k`/`kubectl` and `run` *raw* (bypassing Typer, so arbitrary flags pass through verbatim) before
falling back to the Typer app. The `run` Typer command exists only for `--help` and completion.

**Context.** `cli/state.State` finds the repo (search upwards for `.talkub.yaml`, or `--repo`) and
builds a `talkub.context.Context` (`Repo` + `Runner` + `Keyring` + `Sops`) that is passed into every flow.

**Subprocesses.** `core/runner.Runner` is the *only* place that starts processes. Children get an
environment with key material (`SOPS_AGE_KEY`, `SOPS_AGE_KEY_FILE`, `TALKUB_IDENTITY`) stripped unless
passed back explicitly. Missing binaries raise `ToolMissing` with an install hint.

**Secrets never touch the repo in plaintext.** Decrypted talosconfig/kubeconfig/machine configs live
only in private temp dirs from `core/ephemeral.py`, which are removed on exit, including on
SIGTERM/SIGHUP. Machine configs are rendered on the fly from `talos/secrets.sops.yaml`,
`talos/patches/*` and `talos/nodes/<node>.yaml`, never written to disk in the cluster repo.

**Errors.** Raise subclasses of `core/errors.TalkubError` (`message` + optional `hint`); `main()` prints
them as `error:`/`hint:` without a traceback (traceback only with `--debug`).

**Single sources of truth in the cluster repo.** Chart versions live only in the Argo CD app files
`gitops/apps/*.yaml` (`targetRevision`); Talos/Kubernetes target versions and schematics live in
`.talkub.yaml`. YAML is edited with ruamel round-tripping (`core/yamlio.py`) so users' comments survive.

## Tests

- `tests/conftest.py` has an autouse fixture that strips key/kube/talos/proxy env vars and points
  `XDG_CONFIG_HOME` at `tmp_path` — tests must never touch the developer's real keys or configs.
- External tools are faked, not mocked in-process: `fake_bin` puts a shell script in front of `PATH`,
  `spy("kubectl")` installs a logging fake whose calls can be asserted; `helpers.FakeRunner` records
  argv instead of running. Tests needing real binaries (`age`, `sops`, `talosctl`, …) skip via
  `helpers.need(...)` when they are missing.
- Fixtures `empty_repo`, `software_identity`, `keyed_repo` build a cluster repo with real age keys.
- talkub imports inside fixtures are lazy on purpose.

## Workflow conventions

- Code, comments and CLI strings in English.
- Features follow a spec → plan → implementation flow; specs and plans are dated files under
  `docs/superpowers/specs/` and `docs/superpowers/plans/`. Read the relevant spec before changing a feature.
- Commit messages use Conventional Commits (`feat(volumes): …`, `fix(cli): …`, `refactor: …`, `docs: …`).
