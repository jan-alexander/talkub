"""kubeconfig."""

from __future__ import annotations

import typer

from talkub.talos import kubeconfig as kubeconfig_flow


def kubeconfig(ctx: typer.Context) -> None:
    """Fetch a fresh admin kubeconfig from a control plane into kubernetes/kubeconfig.sops.yaml.

    Nothing else in the cluster is touched; use it when the stored kubeconfig no longer works.
    """
    context = ctx.obj.context()
    kubeconfig_flow.refresh(context)
    print(f"next: commit {context.repo.rel(context.repo.kubeconfig)}")
