"""chart upgrade."""

from __future__ import annotations

import typer

from talkub.gitops import chart_upgrade

app = typer.Typer(help="Manage the Helm charts behind gitops/apps/*.yaml.", no_args_is_help=True)


@app.command("upgrade")
def upgrade(
    ctx: typer.Context,
    app_name: str = typer.Argument(None, metavar="APP", help="Chart app to upgrade (see `talkub versions`)."),
    all_apps: bool = typer.Option(False, "--all", help="Upgrade every chart app."),
    version: str = typer.Option(None, "--version", help="Target version (default: the latest)."),
    yes: bool = typer.Option(False, "--yes", help="Do not ask before upgrading."),
) -> None:
    """Bump one Argo CD app's Helm chart (or, with --all, every chart app) to a new version."""
    code = chart_upgrade.upgrade(
        ctx.obj.context(),
        app_name,
        all_apps=all_apps,
        version=version,
        confirm=lambda question: yes or typer.confirm(question, default=True),
    )
    raise typer.Exit(code)
