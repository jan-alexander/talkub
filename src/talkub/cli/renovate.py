"""renovate init."""

from __future__ import annotations

import typer

from talkub.gitops import renovate

app = typer.Typer(
    help="Generate a Renovate config for chart, Talos and Kubernetes update PRs.",
    no_args_is_help=True,
)


@app.command("init")
def init(
    ctx: typer.Context,
    force: bool = typer.Option(
        False, "--force", help="Overwrite an existing renovate.json."
    ),
) -> None:
    """Write renovate.json in the repo root."""
    path = renovate.init(ctx.obj.repo(), force=force)
    print(f"wrote {path}")
    for note in renovate.NOTES:
        print(f"note: {note}")
