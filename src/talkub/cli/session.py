"""shell, plus help-only stubs for the talosctl/kubectl pass-through.

The real pass-through happens in main.main() before Typer parses anything,
because Typer would swallow `--` and intercept `--help`.
"""

from __future__ import annotations

import typer

from talkub import access

PASSTHROUGH = {"allow_extra_args": True, "ignore_unknown_options": True, "help_option_names": []}


def register(app: typer.Typer) -> None:
    @app.command("talosctl", context_settings=PASSTHROUGH)
    def talosctl_cmd(ctx: typer.Context) -> None:
        """Run talosctl with the decrypted talosconfig (alias: t)."""
        raise typer.Exit(access.talosctl(ctx.obj.context(), ctx.args))

    @app.command("t", context_settings=PASSTHROUGH, hidden=True)
    def t_cmd(ctx: typer.Context) -> None:
        raise typer.Exit(access.talosctl(ctx.obj.context(), ctx.args))

    @app.command("kubectl", context_settings=PASSTHROUGH)
    def kubectl_cmd(ctx: typer.Context) -> None:
        """Run kubectl with the decrypted kubeconfig (alias: k)."""
        raise typer.Exit(access.kubectl(ctx.obj.context(), ctx.args))

    @app.command("k", context_settings=PASSTHROUGH, hidden=True)
    def k_cmd(ctx: typer.Context) -> None:
        raise typer.Exit(access.kubectl(ctx.obj.context(), ctx.args))

    @app.command("shell")
    def shell_cmd(
        ctx: typer.Context,
        program: str = typer.Option(None, "--program", help="Shell to start (default: $SHELL)."),
    ) -> None:
        """Start a subshell with TALOSCONFIG and KUBECONFIG set; removed on exit."""
        raise typer.Exit(access.shell(ctx.obj.context(), program))
