"""Helm charts installed by bootstrap and managed by ArgoCD afterwards."""

from __future__ import annotations

from talkub import yamlio
from talkub.repo import ChartSpec
from talkub.runner import Runner

DEFAULT_CHARTS: dict[str, tuple[str, str]] = {
    "argocd": ("https://argoproj.github.io/argo-helm", "argo-cd"),
    "sealed-secrets": ("https://bitnami.github.io/sealed-secrets", "sealed-secrets"),
}

# Charts installed only when the corresponding feature is enabled (CoreDNS takeover,
# Tailscale operator), resolved on demand via `resolve`.
OPTIONAL_CHARTS: dict[str, tuple[str, str]] = {
    "coredns": ("https://coredns.github.io/helm", "coredns"),
    "tailscale-operator": ("https://pkgs.tailscale.com/helmcharts", "tailscale-operator"),
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


def resolve(runner: Runner, key: str) -> ChartSpec:
    """Latest version for one chart, from `DEFAULT_CHARTS` or `OPTIONAL_CHARTS`."""
    repo, chart = (DEFAULT_CHARTS | OPTIONAL_CHARTS)[key]
    return ChartSpec(repo=repo, chart=chart, version=latest_version(runner, repo, chart))
