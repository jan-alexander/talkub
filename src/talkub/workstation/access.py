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


PIPE_NO_CREDENTIALS = "no-credentials-in-pipe"


def _require_stages(pipe: Sequence[Sequence[str]]) -> None:
    for stage in pipe:
        require_tool(stage[0])


def _launch(
    ctx: Context, argv: Sequence[str], env: dict[str, str], pipe: Sequence[Sequence[str]], workdir: Path
) -> int:
    """Run `argv` with `env`; with pipe stages, feed its output through them. The stages get no credentials,
    and KUBECONFIG/TALOSCONFIG point at a missing file so a kubectl or talosctl there fails instead of
    silently using the caller's own default cluster."""
    if not pipe:
        return ctx.runner.call(argv, env=env)
    missing = str(workdir / PIPE_NO_CREDENTIALS)
    return ctx.runner.pipeline([argv, *pipe], env=env, rest_env={"KUBECONFIG": missing, "TALOSCONFIG": missing})


def talosctl(ctx: Context, args: Sequence[str], pipe: Sequence[Sequence[str]] = ()) -> int:
    require_tool("talosctl")
    _require_stages(pipe)
    source = _existing(ctx, ctx.repo.talosconfig, TALOSCONFIG_HINT)
    with decrypted(ctx.sops, {"talosconfig": source}) as paths:
        talosconfig = paths["talosconfig"]
        return _launch(ctx, ["talosctl", *args], {"TALOSCONFIG": str(talosconfig)}, pipe, talosconfig.parent)


def kubectl(ctx: Context, args: Sequence[str], pipe: Sequence[Sequence[str]] = ()) -> int:
    require_tool("kubectl")
    _require_stages(pipe)
    source = _existing(ctx, ctx.repo.kubeconfig, KUBECONFIG_HINT)
    with decrypted(ctx.sops, {"kubeconfig": source}) as paths:
        kubeconfig = paths["kubeconfig"]
        return _launch(ctx, ["kubectl", *args], {"KUBECONFIG": str(kubeconfig)}, pipe, kubeconfig.parent)


KUBECONFIG_PLACEHOLDER = "{kubeconfig}"


def with_kubeconfig(
    ctx: Context, argv: Sequence[str], extra: Sequence[str] = (), pipe: Sequence[Sequence[str]] = ()
) -> int:
    """Run any program with the decrypted kubeconfig in KUBECONFIG. In `argv` (not in `extra` or `pipe`),
    every occurrence of {kubeconfig} is replaced by the file's path."""
    program, *args = argv
    require_tool(program)
    _require_stages(pipe)
    source = _existing(ctx, ctx.repo.kubeconfig, KUBECONFIG_HINT)
    with decrypted(ctx.sops, {"kubeconfig": source}) as paths:
        kubeconfig = paths["kubeconfig"]
        filled = [arg.replace(KUBECONFIG_PLACEHOLDER, str(kubeconfig)) for arg in args]
        return _launch(ctx, [program, *filled, *extra], {"KUBECONFIG": str(kubeconfig)}, pipe, kubeconfig.parent)


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
