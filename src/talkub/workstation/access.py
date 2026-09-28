"""Run talosctl, kubectl or a shell with credentials decrypted only for the duration.

Each entry point checks its tool first, so a missing binary is reported before
anything is decrypted (or a hardware key has to be touched).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from pathlib import Path

from talkub.context import Context
from talkub.core.ephemeral import decrypted
from talkub.core.errors import ValidationError
from talkub.core.runner import require_tool

TALOSCONFIG_HINT = "it is created by `talkub init`"
KUBECONFIG_HINT = "it is created by `talkub bootstrap`"


def _existing(ctx: Context, path: Path, hint: str) -> Path:
    if not path.is_file():
        raise ValidationError(f"{ctx.repo.rel(path)} does not exist", hint=hint)
    return path


def talosctl(ctx: Context, args: Sequence[str]) -> int:
    require_tool("talosctl")
    source = _existing(ctx, ctx.repo.talosconfig, TALOSCONFIG_HINT)
    with decrypted(ctx.sops, {"talosconfig": source}) as paths:
        return ctx.runner.call(["talosctl", *args], env={"TALOSCONFIG": str(paths["talosconfig"])})


def kubectl(ctx: Context, args: Sequence[str]) -> int:
    require_tool("kubectl")
    source = _existing(ctx, ctx.repo.kubeconfig, KUBECONFIG_HINT)
    with decrypted(ctx.sops, {"kubeconfig": source}) as paths:
        return ctx.runner.call(["kubectl", *args], env={"KUBECONFIG": str(paths["kubeconfig"])})


KUBECONFIG_PLACEHOLDER = "{kubeconfig}"


def with_kubeconfig(ctx: Context, argv: Sequence[str], extra: Sequence[str] = ()) -> int:
    """Run any program with the decrypted kubeconfig in KUBECONFIG. In `argv` (not in `extra`),
    every occurrence of {kubeconfig} is replaced by the file's path."""
    program, *args = argv
    require_tool(program)
    source = _existing(ctx, ctx.repo.kubeconfig, KUBECONFIG_HINT)
    with decrypted(ctx.sops, {"kubeconfig": source}) as paths:
        kubeconfig = str(paths["kubeconfig"])
        filled = [arg.replace(KUBECONFIG_PLACEHOLDER, kubeconfig) for arg in args]
        return ctx.runner.call([program, *filled, *extra], env={"KUBECONFIG": kubeconfig})


def shell(ctx: Context, program: str | None = None) -> int:
    if active := os.environ.get("TALKUB_SESSION"):
        raise ValidationError(f"already inside a talkub shell for '{active}'", hint="exit it first")
    program = program or os.environ.get("SHELL") or "/bin/sh"
    require_tool(program)
    name = ctx.repo.load_settings().name
    files = {"talosconfig": _existing(ctx, ctx.repo.talosconfig, TALOSCONFIG_HINT)}
    if ctx.repo.kubeconfig.is_file():
        files["kubeconfig"] = ctx.repo.kubeconfig
    with decrypted(ctx.sops, files) as paths:
        workdir = paths["talosconfig"].parent
        env = {
            "TALOSCONFIG": str(paths["talosconfig"]),
            # Without a kubeconfig, point at a missing file so kubectl cannot silently
            # talk to whatever cluster the parent KUBECONFIG selects.
            "KUBECONFIG": str(paths.get("kubeconfig", workdir / "no-kubeconfig-yet")),
            "TALKUB_SESSION": name,
        }
        print(
            f"talkub: shell for '{name}' - credentials are removed on exit",
            file=sys.stderr,
        )
        code = ctx.runner.call([program], env=env)
    print("talkub: credentials removed", file=sys.stderr)
    return code
