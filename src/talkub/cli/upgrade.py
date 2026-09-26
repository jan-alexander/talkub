"""upgrade talos, upgrade k8s."""

from __future__ import annotations

import typer

from talkub.talos import upgrade as upgrade_flow

app = typer.Typer(
    help="Upgrade Talos or Kubernetes to the versions in .talkub.yaml.",
    no_args_is_help=True,
)
YES = typer.Option(False, "--yes", help="Do not ask before upgrading.")


def _confirm(yes: bool):
    def confirm(question: str) -> bool:
        return yes or typer.confirm(question, default=False)

    return confirm


@app.command("talos")
def talos(
    ctx: typer.Context,
    nodes: list[str] = typer.Argument(
        None, help="Nodes (default: all, control planes first)."
    ),
    yes: bool = YES,
) -> None:
    """Upgrade nodes whose Talos version or schematic differs (one at a time)."""
    upgrade_flow.talos(ctx.obj.context(), nodes or [], confirm=_confirm(yes))


@app.command("k8s")
def k8s(ctx: typer.Context, yes: bool = YES) -> None:
    """Upgrade Kubernetes (shows the talosctl dry-run first)."""
    if not upgrade_flow.k8s(ctx.obj.context(), confirm=_confirm(yes)):
        raise typer.Exit(1)
