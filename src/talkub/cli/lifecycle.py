"""plan, apply."""

from __future__ import annotations

import typer

from talkub import lifecycle
from talkub.errors import ValidationError

NODES = typer.Argument(None, help="Nodes (default: all, control planes first).")


def plan(ctx: typer.Context, nodes: list[str] = NODES) -> None:
    """Render configs and show what `apply` would change (Talos dry-run)."""
    lifecycle.plan(ctx.obj.context(), nodes or [])


def apply(
    ctx: typer.Context,
    nodes: list[str] = NODES,
    mode: str = typer.Option("auto", "--mode", help="auto, no-reboot, staged or try."),
    yes: bool = typer.Option(False, "--yes", help="Do not ask before applying."),
    insecure: bool = typer.Option(
        False, "--insecure", help="First install of ONE node in maintenance mode."
    ),
    force_unhealthy: bool = typer.Option(
        False,
        "--force-unhealthy",
        help="Skip the pre-apply health check (use when the pending change is what fixes the cluster).",
    ),
) -> None:
    """Apply rendered configs to nodes (asks per node unless --yes)."""
    context = ctx.obj.context()

    def confirm(question: str) -> bool:
        return yes or typer.confirm(question, default=False)

    if insecure:
        if len(nodes or []) != 1:
            raise ValidationError(
                "--insecure needs exactly one node",
                hint="e.g. talkub apply --insecure cp-01",
            )
        if mode != "auto":
            raise ValidationError(
                "--mode does not apply to --insecure",
                hint="the first install always reboots into the installed system",
            )
        if force_unhealthy:
            raise ValidationError(
                "--force-unhealthy does not apply to --insecure",
                hint="the first install has no health pre-check to skip",
            )
        if not lifecycle.apply_insecure(context, nodes[0], confirm=confirm):
            raise typer.Exit(1)
        return
    lifecycle.apply(
        context, nodes or [], mode=mode, confirm=confirm, skip_precheck=force_unhealthy
    )
