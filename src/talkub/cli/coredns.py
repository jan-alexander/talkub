"""coredns takeover."""

from __future__ import annotations

import typer

from talkub.gitops import coredns

app = typer.Typer(help="CoreDNS as a Helm release managed by Argo CD.", no_args_is_help=True)


@app.command("takeover")
def takeover(
    ctx: typer.Context,
    resume: bool = typer.Option(
        False,
        "--resume",
        help="Continue an interrupted takeover (repo files already written).",
    ),
    yes: bool = typer.Option(False, "--yes", help="Do not ask for confirmation."),
) -> None:
    """Move CoreDNS on the running cluster from Talos to Argo CD (DNS keeps working)."""
    context = ctx.obj.context()

    def confirm(question: str) -> bool:
        return yes or typer.confirm(question, default=False)

    for note in coredns.takeover(context, resume=resume, confirm=confirm):
        print(f"note: {note}")
