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
