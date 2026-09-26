"""bootstrap."""

from __future__ import annotations

import typer

from talkub.gitops import bootstrap as bootstrap_flow


def bootstrap(ctx: typer.Context) -> None:
    """Bootstrap etcd, fetch the kubeconfig, install Sealed Secrets and Argo CD, apply the root app.

    Safe to re-run after a failure; finished steps are detected and skipped.
    """
    bootstrap_flow.run(ctx.obj.context())
