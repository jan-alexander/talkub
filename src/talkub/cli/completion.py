"""completion: shell completion scripts and dynamic candidates."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

import typer
from typer.completion import completion_init, get_completion_script

from talkub.config import find_repo
from talkub.config.shortcuts import needs_node
from talkub.core.errors import TalkubError
from talkub.workstation import shortcuts

PROG_NAME = "talkub"
COMPLETE_VAR = "_TALKUB_COMPLETE"

# The app is built with add_completion=False, so Typer never registers its shell classes on its own;
# without them the _TALKUB_COMPLETE requests from the scripts below fail with "Shell ... not supported".
completion_init()


class Shell(StrEnum):
    zsh = "zsh"
    bash = "bash"
    fish = "fish"


def completion(shell: Shell = typer.Argument(..., help="Target shell.")) -> None:
    """Print the completion script, e.g. `eval "$(talkub completion zsh)"` in ~/.zshrc."""
    print(get_completion_script(prog_name=PROG_NAME, complete_var=COMPLETE_VAR, shell=shell.value))


def _repo_option(ctx: typer.Context) -> Path | None:
    """The --repo already typed on the command line; the global callback does not run while completing."""
    root = ctx.find_root()
    value = root.params.get("repo")
    return Path(value) if value else None


def shortcut_names(ctx: typer.Context, incomplete: str) -> list[tuple[str, str]]:
    """Candidates for `talkub run <TAB>`: (name, help text) of the project and global shortcuts."""
    try:
        entries = shortcuts.available(find_repo(Path.cwd(), _repo_option(ctx)))
    except TalkubError:
        return []  # no repo or a broken config: offer nothing rather than a traceback in the prompt
    return [
        (name, shortcut.description or shortcut.kind)
        for name, (shortcut, _) in sorted(entries.items())
        if name.startswith(incomplete)
    ]


def node_names(ctx: typer.Context, incomplete: str) -> list[tuple[str, str]]:
    """Candidates for `talkub run <name> <TAB>`: node names, if <name> is a shortcut with {node} placeholders."""
    name = ctx.params.get("name")
    try:
        repo = find_repo(Path.cwd(), _repo_option(ctx))
        entry = shortcuts.available(repo).get(name) if name else None
        if entry is None or not needs_node(entry[0]):
            return []
        nodes = repo.load_settings().nodes
    except TalkubError:
        return []
    return [
        (node, f"{spec.role} {spec.mgmt or spec.ip}")
        for node, spec in sorted(nodes.items())
        if node.startswith(incomplete)
    ]
