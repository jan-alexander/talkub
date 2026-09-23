"""keys rotate."""

from __future__ import annotations

import typer

app = typer.Typer(help="Manage the cluster master key.", no_args_is_help=True)


@app.command("rotate")
def rotate(
    ctx: typer.Context,
    yes: bool = typer.Option(False, "--yes", help="Do not ask for confirmation."),
) -> None:
    """Create a new master key and re-encrypt every *.sops.yaml."""
    context = ctx.obj.context()
    count = len(context.repo.sops_files())
    if not yes and not typer.confirm(f"Rotate the master key and re-encrypt {count} file(s)?"):
        raise typer.Exit(1)
    context.keyring.rotate(context.sops)
    print(f"rotated; re-encrypted {count} file(s). Commit all changes together.")
