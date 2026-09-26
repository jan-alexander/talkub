"""The `talkub` pre-commit hook: `talkub hooks install`/`uninstall`."""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from pathlib import Path

from talkub.errors import ValidationError
from talkub.fsutil import atomic_write
from talkub.lint import DEFAULT_YAMLLINT
from talkub.repo import Repo
from talkub.runner import Runner

MARKER = "talkub pre-commit hook"


def talkub_path() -> str:
    """The absolute path of the running talkub program, or `"talkub"` if it cannot be resolved."""
    found = shutil.which(sys.argv[0])
    if found:
        return str(Path(found).resolve())
    candidate = Path(sys.argv[0])
    if candidate.exists() and os.access(candidate, os.X_OK):
        return str(candidate.resolve())
    return "talkub"


def render_hook(talkub: str) -> str:
    return (
        "#!/bin/sh\n"
        f"# {MARKER} (managed by talkub; reinstall with `talkub hooks install`)\n"
        f"TALKUB={shlex.quote(talkub)}\n"
        '[ -x "$TALKUB" ] || TALKUB=talkub\n'
        '"$TALKUB" check && "$TALKUB" lint --offline\n'
    )


def hook_path(repo: Repo, runner: Runner) -> Path:
    """`<git-dir>/hooks/pre-commit`; raises if `repo.root` is not a git repository."""
    proc = runner.run(["git", "rev-parse", "--git-dir"], cwd=repo.root, check=False)
    if proc.returncode != 0:
        raise ValidationError(f"{repo.root} is not a git repository", hint="git init")
    git_dir = Path(proc.stdout.decode(errors="replace").strip())
    if not git_dir.is_absolute():
        git_dir = repo.root / git_dir
    return git_dir / "hooks" / "pre-commit"


def install(repo: Repo, runner: Runner, *, force: bool = False) -> list[str]:
    """Install (or reinstall) the pre-commit hook. Returns notes for the caller to print.

    A hook file that already exists and was not written by talkub (no MARKER) is left alone
    unless `force` is set.
    """
    path = hook_path(repo, runner)
    if path.is_file() and MARKER not in path.read_text() and not force:
        raise ValidationError(
            "a pre-commit hook already exists", hint="talkub hooks install --force"
        )
    atomic_write(path, render_hook(talkub_path()).encode(), mode=0o755)

    notes: list[str] = []
    yamllint_path = repo.root / ".yamllint"
    if not yamllint_path.is_file():
        atomic_write(yamllint_path, DEFAULT_YAMLLINT.encode())
        notes.append("wrote .yamllint — commit it")
    return notes


def uninstall(repo: Repo, runner: Runner) -> Path:
    """Remove the pre-commit hook if it is talkub's own (has MARKER); raises otherwise."""
    path = hook_path(repo, runner)
    if not path.is_file() or MARKER not in path.read_text():
        raise ValidationError("no talkub pre-commit hook is installed")
    path.unlink()
    return path
