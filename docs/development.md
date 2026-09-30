# Development

    uv run pytest
    uv run ruff check src tests
    uv run lint-imports

Enable the pre-commit hook (import layers, ruff check, ruff format) once per clone:

    git config core.hooksPath .githooks

## Code layout

`src/talkub/` is split by domain. `cli/` holds one thin module per command group and calls into
the packages below it. Each package's `__init__.py` says what it covers.

| Package        | What lives there                                                                                                  |
| -------------- | ----------------------------------------------------------------------------------------------------------------- |
| `cli/`         | Typer commands, argument parsing, prompts, console output                                                         |
| `scaffold.py`  | `talkub init`: writes a new cluster repo from `templates/`                                                        |
| `overview/`    | `status`, `versions`: read-only views across the cluster                                                          |
| `gitops/`      | Flows that change what Argo CD deploys: bootstrap, chart upgrade, git repo, CoreDNS, Tailscale operator, Renovate |
| `workstation/` | The admin's machine: credential shells, shortcuts, clipboard                                                      |
| `quality/`     | `lint`, `check`, pre-commit hook                                                                                  |
| `helm/`        | Chart catalog and `helm` command lines                                                                            |
| `talos/`       | Machine configs, node lifecycle, upgrades, CA rotation, installer images, Tailscale on the hosts                  |
| `argocd/`      | Rendering, parsing and editing `gitops/apps/*.yaml`                                                               |
| `kube/`        | kubectl/helm sessions, kubeseal                                                                                   |
| `context.py`   | `Context`: repo + runner + keys, passed to every flow                                                             |
| `secrets/`     | age identities, recipients, master key, SOPS                                                                      |
| `config/`      | The repo on disk and `.talkub.yaml` (`Repo`, `Settings`, specs)                                                   |
| `core/`        | Errors, running tools, YAML, atomic writes, temp files                                                            |

The table is ordered top to bottom by layer: a package may import the ones below it, never the
ones above. `uv run lint-imports` enforces this (contract in `pyproject.toml`).

Adding a command: put the logic into the package that owns the domain, add a thin module in
`cli/`, register it in `cli/main.py`, and put the tests into the matching folder under `tests/`.

## Releases

Releases are created by the manually triggered `Release` workflow (Actions tab, or
`gh workflow run release.yml`): it takes the version from `pyproject.toml`, aborts if release
`v<version>` already exists, runs lint and tests, then publishes wheel, sdist, the binaries and
`SHA256SUMS`. Bump the version first with `uv version --bump patch|minor|major`.
