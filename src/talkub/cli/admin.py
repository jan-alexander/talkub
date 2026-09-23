"""admin add / remove / list."""

from __future__ import annotations

import typer

from talkub.recipients import Recipient, validate_key, validate_name

app = typer.Typer(help="Manage who can decrypt the cluster master key.", no_args_is_help=True)


@app.command("add")
def add(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="age public key (age1...)."),
    name: str = typer.Option(..., "--name", help="Admin name."),
) -> None:
    """Add a recipient and re-encrypt keys/master.age."""
    context = ctx.obj.context()
    context.keyring.add(Recipient(validate_name(name), validate_key(key)))
    print(f"added {name}; commit keys/ to share access")


@app.command("remove")
def remove(ctx: typer.Context, name: str = typer.Argument(..., help="Admin name.")) -> None:
    """Remove a recipient and re-encrypt keys/master.age."""
    ctx.obj.context().keyring.remove(name)
    print(f"removed {name}. They may still know the old master key: run `talkub keys rotate`.")


@app.command("list")
def list_(ctx: typer.Context) -> None:
    """List recipients."""
    items = ctx.obj.context().keyring.recipients()
    for item in items:
        print(f"{item.name}\t{item.key}")
    if len(items) < 2:
        print("warning: fewer than 2 recipients - losing one key locks you out")
