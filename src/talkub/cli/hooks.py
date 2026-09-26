"""hooks install/uninstall."""

from __future__ import annotations

import typer

from talkub.cli.output import print_notes
from talkub.core.runner import Runner
from talkub.quality import hooks

app = typer.Typer(
    help="Manage the pre-commit hook (talkub check && talkub lint --offline).",
    no_args_is_help=True,
)


@app.command("install")
def install(
    ctx: typer.Context,
    force: bool = typer.Option(
        False, "--force", help="Overwrite a pre-commit hook not managed by talkub."
    ),
) -> None:
    """Install the pre-commit hook into the repo's git hooks directory."""
    repo = ctx.obj.repo()
    runner = Runner()
    notes = hooks.install(repo, runner, force=force)
    print(f"installed {hooks.hook_path(repo, runner)}")
    print_notes(notes)


@app.command("uninstall")
def uninstall(ctx: typer.Context) -> None:
    """Remove the talkub pre-commit hook."""
    path = hooks.uninstall(ctx.obj.repo(), Runner())
    print(f"removed {path}")
