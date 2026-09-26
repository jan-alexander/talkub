"""sops edit / decrypt with the resolved master key."""

from __future__ import annotations

import sys
from pathlib import Path

import typer

app = typer.Typer(
    help="Edit or decrypt SOPS files with the cluster master key.", no_args_is_help=True
)


@app.command("edit")
def edit(ctx: typer.Context, file: Path) -> None:
    """Open a SOPS file in $EDITOR (creates it if missing)."""
    context = ctx.obj.context()
    code = context.sops.edit(
        file.resolve(),
        context.keyring.public_key(),
        retry=lambda question: typer.confirm(question, default=True),
    )
    raise typer.Exit(code)


@app.command("decrypt")
def decrypt(ctx: typer.Context, file: Path) -> None:
    """Print the decrypted content to stdout."""
    sys.stdout.buffer.write(ctx.obj.context().sops.decrypt(file.resolve()))
    sys.stdout.buffer.flush()
