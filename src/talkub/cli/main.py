"""Entry point: global options, error reporting and raw pass-through for t/k/run."""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

import typer

from talkub import __version__
from talkub.cli import admin, check, completion, image, keys, lint, secret, session, setup
from talkub.cli import bootstrap as bootstrap_cli
from talkub.cli import chart as chart_cli
from talkub.cli import coredns as coredns_cli
from talkub.cli import hooks as hooks_cli
from talkub.cli import lifecycle as lifecycle_cli
from talkub.cli import nodes as nodes_cli
from talkub.cli import renovate as renovate_cli
from talkub.cli import repo as repo_cli
from talkub.cli import rotate as rotate_cli
from talkub.cli import sops as sops_cli
from talkub.cli import status as status_cli
from talkub.cli import tailscale as tailscale_cli
from talkub.cli import upgrade as upgrade_cli
from talkub.cli import versions as versions_cli
from talkub.cli.state import State
from talkub.config import MARKER
from talkub.config.user import display_path, global_shortcuts_file
from talkub.core.errors import TalkubError, ValidationError
from talkub.workstation import access, clipboard, clipboard_clear, shortcuts

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
app.add_typer(secret.app, name="secret")
app.add_typer(nodes_cli.app, name="node")
app.command("check")(check.check)
app.command("lint")(lint.lint)
app.add_typer(hooks_cli.app, name="hooks")
app.command("plan")(lifecycle_cli.plan)
app.command("apply")(lifecycle_cli.apply)
app.command("bootstrap")(bootstrap_cli.bootstrap)
app.add_typer(upgrade_cli.app, name="upgrade")
app.add_typer(chart_cli.app, name="chart")
app.command("status")(status_cli.status)
app.add_typer(versions_cli.app, name="versions")
app.add_typer(tailscale_cli.app, name="tailscale")
app.add_typer(repo_cli.app, name="repo")
app.add_typer(renovate_cli.app, name="renovate")
app.add_typer(coredns_cli.app, name="coredns")
app.command("rotate-ca")(rotate_cli.rotate_ca)
app.command("completion")(completion.completion)

RUN_USAGE = """\
usage: talkub run                                       list the shortcuts (project and global)
       talkub run [--print] [--follow] <name> [args...]  run one
       talkub run --save-global [--force] <name>         move a project shortcut to the global pool

  --print        secret: write the value to stdout instead of the clipboard
  --follow       job: wait for the pod, stream its logs, exit with the job's result
  --save-global  move <name> from .talkub.yaml to ~/.config/talkub/shortcuts.yaml
  --force        with --save-global: replace a global shortcut of the same name

Shortcuts come from .talkub.yaml and the global pool ($XDG_CONFIG_HOME/talkub/shortcuts.yaml);
a project shortcut hides a global one with the same name. Options go before <name>; arguments
after it are passed on verbatim (run and kubernetes shortcuts only)."""


@app.command("run", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def _run_help(
    # Declared only for --help and shell completion; run_shortcut() parses the real arguments.
    name: str = typer.Argument(None, autocompletion=completion.shortcut_names, help="Shortcut to run."),
    print_value: bool = typer.Option(False, "--print", help="secret: write the value to stdout."),
    follow: bool = typer.Option(False, "--follow", help="job: stream the logs, exit with the job's result."),
    save_global: bool = typer.Option(False, "--save-global", help="Move <name> to the global pool."),
    force: bool = typer.Option(False, "--force", help="With --save-global: replace a global shortcut."),
) -> None:
    """Run a shortcut from .talkub.yaml or the global pool; without a name, list them."""
    # Never reached via main(), which handles `run` raw (like t/k) so options after the name pass through.
    print(RUN_USAGE)


_RUN_FLAGS = ("--print", "--follow", "--save-global", "--force")


def run_shortcut(state: State, args: list[str]) -> int:
    """`talkub run [flags] [<name> [args...]]`, parsed raw: flags only before the name."""
    flags: set[str] = set()
    index = 0
    while index < len(args) and args[index] in (*_RUN_FLAGS, "--help", "-h"):
        if args[index] in ("--help", "-h"):
            print(RUN_USAGE)
            return 0
        flags.add(args[index])
        index += 1
    save = "--save-global" in flags
    if "--force" in flags and not save:
        raise ValidationError("--force only applies to --save-global")
    if save and flags & {"--print", "--follow"}:
        raise ValidationError("--save-global cannot be combined with --print or --follow")

    if index == len(args):
        if save:
            raise ValidationError("--save-global needs a shortcut name")
        lines = shortcuts.listing(shortcuts.available(state.repo()))
        where = display_path(global_shortcuts_file())
        print("\n".join(lines) if lines else f"no shortcuts defined in {MARKER} or {where}")
        return 0

    name, extra = args[index], args[index + 1 :]
    if save:
        if extra:
            raise ValidationError("--save-global takes only the shortcut name", hint="options go before the name")
        path = shortcuts.save_global(state.repo(), name, force="--force" in flags)
        print(f"moved {name} to {display_path(path)}")
        return 0
    return shortcuts.run(
        state.context(),
        name,
        extra,
        print_value="--print" in flags,
        follow="--follow" in flags,
    )


PASSTHROUGH = {
    "t": access.talosctl,
    "talosctl": access.talosctl,
    "k": access.kubectl,
    "kubectl": access.kubectl,
}


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
    if argv == [clipboard.CLEAR_COMMAND]:
        # A compiled binary has no `python -m`; the detached clearer re-enters here instead.
        clipboard_clear.main()
        return
    state, rest = split_globals(argv)
    try:
        if rest and rest[0] in PASSTHROUGH:
            raise SystemExit(PASSTHROUGH[rest[0]](state.context(), rest[1:]))
        if rest and rest[0] == "run":
            raise SystemExit(run_shortcut(state, rest[1:]))
        app(args=argv, prog_name="talkub")
    except TalkubError as err:
        _report(err, state.debug)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        # Ctrl-C (e.g. while waiting for a YubiKey touch): no traceback, shell-style code.
        print("interrupted", file=sys.stderr)
        raise SystemExit(130) from None
