"""Helm charts talkub writes Argo CD apps for; the apps under gitops/apps/ pin their versions."""

from __future__ import annotations

from talkub.config import ChartSpec
from talkub.core import yamlio
from talkub.core.runner import Runner

# chart key (= gitops/apps/<key>.yaml) -> (Helm repo, chart name); used only to create new app
# files (`init`, `coredns takeover`, `tailscale operator enable`) at the latest chart version.
CATALOG: dict[str, tuple[str, str]] = {
    "argocd": ("https://argoproj.github.io/argo-helm", "argo-cd"),
    "sealed-secrets": ("https://bitnami.github.io/sealed-secrets", "sealed-secrets"),
    "coredns": ("https://coredns.github.io/helm", "coredns"),
    "tailscale-operator": (
        "https://pkgs.tailscale.com/helmcharts",
        "tailscale-operator",
    ),
}

# The charts every repo gets from `init` (its templates under gitops/apps/).
DEFAULT_KEYS = ("argocd", "sealed-secrets")


def show_chart(runner: Runner, repo: str, chart: str, version: str | None = None) -> dict:
    """Full `helm show chart` document (`version`, `appVersion`, ...); pinned to `version` if
    given, else the repo's latest."""
    if repo.startswith("oci://"):
        argv = ["helm", "show", "chart", f"{repo.rstrip('/')}/{chart}"]
    else:
        argv = ["helm", "show", "chart", chart, "--repo", repo]
    if version is not None:
        argv = [*argv, "--version", version]
    return yamlio.load(runner.run(argv).stdout.decode())


def latest_version(runner: Runner, repo: str, chart: str) -> str:
    return str(show_chart(runner, repo, chart)["version"])


def resolve_defaults(runner: Runner) -> dict[str, ChartSpec]:
    """Latest versions of the charts behind `init`'s templated apps (argocd, sealed-secrets)."""
    return {key: resolve(runner, key) for key in DEFAULT_KEYS}


def resolve(runner: Runner, key: str) -> ChartSpec:
    """Latest version for one chart of `CATALOG`."""
    repo, chart = CATALOG[key]
    return ChartSpec(repo=repo, chart=chart, version=latest_version(runner, repo, chart))
