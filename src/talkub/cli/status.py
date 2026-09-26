"""status."""

from __future__ import annotations

import typer

from talkub.overview import status as status_flow


def status(ctx: typer.Context) -> None:
    """Show target vs. actual per node (Talos, schematic, config drift, kubelet) and Argo CD apps."""
    print(status_flow.render(status_flow.collect(ctx.obj.context())))
