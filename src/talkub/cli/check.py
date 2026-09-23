"""check."""

from __future__ import annotations

import typer

from talkub.checks import run_checks


def check(ctx: typer.Context) -> None:
    """Verify encryption and secret hygiene (use as a pre-commit hook)."""
    findings = run_checks(ctx.obj.repo())
    for finding in findings:
        print(f"{finding.level}: {finding.message}")
    if any(f.level == "error" for f in findings):
        raise typer.Exit(1)
    if not findings:
        print("ok")
