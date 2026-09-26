"""lint."""

from __future__ import annotations

import typer

from talkub.context import Context
from talkub.quality.lint import run


def lint(
    ctx: typer.Context,
    offline: bool = typer.Option(
        False, "--offline", help="Skip checks that need network access."
    ),
) -> None:
    """Lint the cluster repo (YAML style; use as a pre-commit hook). No master key needed."""
    report = run(Context.open(ctx.obj.repo()), offline=offline)
    for line in report.render():
        print(line)
    raise typer.Exit(1 if report.errors else 0)
