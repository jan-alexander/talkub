"""Argo CD Application renderers: a Helm chart source or a plain directory of manifests,
both paired with a git `values`/`ref` source per the multi-source pattern already used by
`templates/repo/gitops/apps/sealed-secrets.yaml.tmpl`."""

from __future__ import annotations

from talkub import yamlio
from talkub.repo import ChartSpec

_HEADER = "# Managed by talkub.\n"
_DESTINATION_SERVER = "https://kubernetes.default.svc"


def helm_app(
    name: str,
    chart: ChartSpec,
    git_url: str,
    values_file: str,
    namespace: str,
    *,
    create_namespace: bool = False,
    server_side: bool = True,
) -> str:
    sync_options = []
    if create_namespace:
        sync_options.append("CreateNamespace=true")
    if server_side:
        sync_options.append("ServerSideApply=true")
    sync_policy: dict = {"automated": {"prune": True, "selfHeal": True}}
    if sync_options:
        sync_policy["syncOptions"] = sync_options
    doc = {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {"name": name, "namespace": "argocd"},
        "spec": {
            "project": "default",
            "sources": [
                {
                    "repoURL": chart.repo,
                    "chart": chart.chart,
                    "targetRevision": chart.version,
                    "helm": {"valueFiles": [f"$values/{values_file}"]},
                },
                {"repoURL": git_url, "targetRevision": "HEAD", "ref": "values"},
            ],
            "destination": {"server": _DESTINATION_SERVER, "namespace": namespace},
            "syncPolicy": sync_policy,
        },
    }
    return _HEADER + yamlio.dump(doc)


def directory_app(
    name: str,
    git_url: str,
    path: str,
    namespace: str,
    *,
    skip_dry_run_on_missing: bool = False,
) -> str:
    sync_options = ["ServerSideApply=true"]
    if skip_dry_run_on_missing:
        sync_options.append("SkipDryRunOnMissingResource=true")
    doc = {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {"name": name, "namespace": "argocd"},
        "spec": {
            "project": "default",
            "source": {
                "repoURL": git_url,
                "targetRevision": "HEAD",
                "path": path,
                "directory": {"recurse": True},
            },
            "destination": {"server": _DESTINATION_SERVER, "namespace": namespace},
            "syncPolicy": {
                "automated": {"prune": True, "selfHeal": True},
                "syncOptions": sync_options,
            },
        },
    }
    return _HEADER + yamlio.dump(doc)
