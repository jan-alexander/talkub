"""Argo CD Application renderers: a Helm chart source or a plain directory of manifests,
both paired with a git `values`/`ref` source per the multi-source pattern already used by
`templates/repo/gitops/apps/sealed-secrets.yaml.tmpl`."""

from __future__ import annotations

from talkub import yamlio
from talkub.repo import ChartSpec

_HEADER = "# Managed by talkub.\n"
_DESTINATION_SERVER = "https://kubernetes.default.svc"
# A failed automated sync is not retried unless syncPolicy.retry is set (e.g. an app whose
# CRDs come from another app that has not synced yet).
_RETRY = {"limit": 10, "backoff": {"duration": "10s", "factor": 2, "maxDuration": "3m"}}


def _metadata(name: str, sync_wave: str | None) -> dict:
    metadata: dict = {"name": name, "namespace": "argocd"}
    if sync_wave is not None:
        metadata["annotations"] = {"argocd.argoproj.io/sync-wave": sync_wave}
    return metadata


def helm_app(
    name: str,
    chart: ChartSpec,
    git_url: str,
    values_file: str,
    namespace: str,
    *,
    create_namespace: bool = False,
    server_side: bool = True,
    sync_wave: str | None = None,
) -> str:
    sync_options = []
    if create_namespace:
        sync_options.append("CreateNamespace=true")
    if server_side:
        sync_options.append("ServerSideApply=true")
    sync_policy: dict = {"automated": {"prune": True, "selfHeal": True}}
    if sync_options:
        sync_policy["syncOptions"] = sync_options
    sync_policy["retry"] = _RETRY
    doc = {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": _metadata(name, sync_wave),
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
    create_namespace: bool = False,
    skip_dry_run_on_missing: bool = False,
    ignore_differences: list[dict] | None = None,
    sync_wave: str | None = None,
) -> str:
    sync_options = ["CreateNamespace=true"] if create_namespace else []
    sync_options.append("ServerSideApply=true")
    if skip_dry_run_on_missing:
        sync_options.append("SkipDryRunOnMissingResource=true")
    if ignore_differences:
        sync_options.append(
            "RespectIgnoreDifferences=true"
        )  # selfHeal must not revert those fields
    doc: dict = {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": _metadata(name, sync_wave),
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
                "retry": _RETRY,
            },
        },
    }
    if ignore_differences:
        doc["spec"]["ignoreDifferences"] = ignore_differences
    return _HEADER + yamlio.dump(doc)
