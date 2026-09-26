"""rotate-ca: rotate the Talos API CA."""

from __future__ import annotations

from pathlib import Path

import typer

from talkub.core.errors import ValidationError
from talkub.talos import rotation


def rotate_ca(
    ctx: typer.Context,
    yes: bool = typer.Option(False, "--yes", help="Do not ask before rotating."),
    finish: bool = typer.Option(
        False, "--finish", help="Finish an interrupted rotation."
    ),
    talosconfig: Path = typer.Option(
        None,
        "--talosconfig",
        help="With --finish: plaintext talosconfig left by an interrupted rotation.",
    ),
) -> None:
    """Rotate the Talos API CA and update the talosconfig and secrets bundle in the repo."""
    context = ctx.obj.context()
    if talosconfig is not None and not finish:
        raise ValidationError("--talosconfig needs --finish")
    if yes and finish:
        raise ValidationError(
            "--yes cannot be combined with --finish", hint="--finish does not ask"
        )
    if talosconfig is not None:
        talosconfig = talosconfig.expanduser()
        if not talosconfig.is_file():
            raise ValidationError(f"{talosconfig} is not a file")
    if finish:
        notes = rotation.finish(context, talosconfig)
    else:
        notes = rotation.rotate(
            context,
            confirm=lambda question: yes or typer.confirm(question, default=False),
        )
    for note in notes:
        print(f"note: {note}")
