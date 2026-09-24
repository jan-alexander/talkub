"""Entry point: global options, error reporting and raw pass-through for t/k."""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

import typer

from talkub import __version__, access
from talkub.cli import admin, check, image, keys, session, setup
from talkub.cli import lifecycle as lifecycle_cli
from talkub.cli import nodes as nodes_cli
from talkub.cli import sops as sops_cli
from talkub.cli.state import State
from talkub.errors import TalkubError

app = typer.Typer(
    help="Administer a Talos cluster repo (ArgoCD, SOPS/age, Sealed Secrets).",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)


def _version(value: bool) -> None:
    if value:
        print(__version__)
        raise typer.Exit()


@app.callback()
def _globals(
    ctx: typer.Context,
    repo: Path = typer.Option(None, "--repo", help="Cluster repo (default: search upwards for .talkub.yaml)."),
    debug: bool = typer.Option(False, "--debug", help="Show tracebacks on errors."),
    version: bool = typer.Option(False, "--version", callback=_version, is_eager=True, help="Show the version."),
) -> None:
    ctx.obj = State(repo_path=repo, debug=debug)


session.register(app)
app.add_typer(setup.identity_app, name="identity")
app.command("init")(setup.init)
app.add_typer(admin.app, name="admin")
app.add_typer(keys.app, name="keys")
app.add_typer(sops_cli.app, name="sops")
app.add_typer(image.app, name="image")
app.add_typer(nodes_cli.app, name="node")
app.command("check")(check.check)
app.command("plan")(lifecycle_cli.plan)
app.command("apply")(lifecycle_cli.apply)

PASSTHROUGH = {"t": access.talosctl, "talosctl": access.talosctl, "k": access.kubectl, "kubectl": access.kubectl}


def split_globals(argv: list[str]) -> tuple[State, list[str]]:
    """Consume leading --repo/--debug; the rest belongs to the subcommand."""
    state = State()
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--debug":
            state.debug = True
            index += 1
        elif arg == "--repo" and index + 1 < len(argv):
            state.repo_path = Path(argv[index + 1])
            index += 2
        elif arg.startswith("--repo="):
            state.repo_path = Path(arg.split("=", 1)[1])
            index += 1
        else:
            break
    return state, argv[index:]


def _report(err: TalkubError, debug: bool) -> None:
    print(f"error: {err}", file=sys.stderr)
    if err.hint:
        print(f"hint: {err.hint}", file=sys.stderr)
    if debug:
        traceback.print_exc()


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    state, rest = split_globals(argv)
    try:
        if rest and rest[0] in PASSTHROUGH:
            raise SystemExit(PASSTHROUGH[rest[0]](state.context(), rest[1:]))
        app(args=argv, prog_name="talkub")
    except TalkubError as err:
        _report(err, state.debug)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        # Ctrl-C (e.g. while waiting for a YubiKey touch): no traceback, shell-style code.
        print("interrupted", file=sys.stderr)
        raise SystemExit(130) from None
