"""versions, versions set."""

from __future__ import annotations

import typer

from talkub.overview import versions as versions_flow

app = typer.Typer(
    help="Target vs. running vs. latest versions: Talos, Kubernetes and chart apps."
)


@app.callback(invoke_without_command=True)
def show(
    ctx: typer.Context,
    offline: bool = typer.Option(
        False, "--offline", help="Skip latest-version lookups (no network)."
    ),
) -> None:
    """Show target vs. running vs. latest versions for Talos, Kubernetes and every chart app."""
    if ctx.invoked_subcommand is not None:
        return
    print(
        versions_flow.render(versions_flow.collect(ctx.obj.context(), offline=offline))
    )


@app.command("set")
def set_(
    ctx: typer.Context,
    component: str = typer.Argument(..., help="talos or kubernetes."),
    version: str = typer.Argument(..., help="New target version."),
) -> None:
    """Set the target Talos or Kubernetes version in .talkub.yaml."""
    print(versions_flow.set_version(ctx.obj.repo(), component, version))
