"""plan / apply: send rendered machine configs to nodes."""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence

from talkub import images
from talkub.context import Context
from talkub.ephemeral import private_file
from talkub.errors import CommandFailed, ValidationError
from talkub.factory import FactoryClient
from talkub.repo import Settings
from talkub.runner import require_tool
from talkub.talos.client import TalosSession, talos_session
from talkub.talos.render import install_summary, render_configs

MODES = ("auto", "no-reboot", "staged", "try")
Confirm = Callable[[str], bool]


def ordered_nodes(settings: Settings, names: Sequence[str]) -> list[str]:
    """The requested nodes (default: all), control planes first."""
    for name in names:
        settings.node(name)
    chosen = list(dict.fromkeys(names)) or list(settings.nodes)
    if not chosen:
        raise ValidationError("no nodes defined", hint="add one with `talkub node add`")
    return sorted(chosen, key=lambda n: (settings.nodes[n].role != "controlplane", n))


def _prepare(ctx: Context, names: Sequence[str]) -> tuple[Settings, list[str], dict[str, bytes]]:
    settings = ctx.repo.load_settings()
    order = ordered_nodes(settings, names)
    for name in order:
        platform = settings.node_platform(name)
        if platform not in settings.schematics:
            raise ValidationError(
                f"no schematic for platform '{platform}' (used by node '{name}')",
                hint=f"define it with `talkub image add --platform {platform} …` "
                f"or `talkub image show --platform {platform}`",
            )
    platforms = sorted({settings.node_platform(name) for name in order})
    if images.refresh(settings, FactoryClient(), platforms):
        ctx.repo.write_settings(settings)
        print("talkub: updated schematic IDs in .talkub.yaml", file=sys.stderr)
    return settings, order, render_configs(ctx, settings, order)


def _dry_run(talos: TalosSession, settings: Settings, name: str, config: bytes) -> None:
    address = settings.mgmt_address(name)
    print(f"== {name} ({address})", flush=True)
    with private_file(f"{name}.yaml", config) as path:
        code = talos.call(address, ["apply-config", "--file", str(path), "--dry-run"])
    if code != 0:
        raise CommandFailed(
            f"dry-run failed for node '{name}'", hint=f"talosctl exited with {code}; is {address} reachable?"
        )


def plan(ctx: Context, names: Sequence[str]) -> None:
    settings, order, configs = _prepare(ctx, names)
    with talos_session(ctx) as talos:
        for name in order:
            _dry_run(talos, settings, name, configs[name])


def apply(ctx: Context, names: Sequence[str], *, mode: str = "auto", confirm: Confirm) -> list[str]:
    if mode not in MODES:
        raise ValidationError(f"unknown mode {mode!r}", hint=f"use one of: {', '.join(MODES)}")
    settings, order, configs = _prepare(ctx, names)
    applied: list[str] = []
    with talos_session(ctx) as talos:
        for name in order:
            _dry_run(talos, settings, name, configs[name])
            if not confirm(f"apply this configuration to {name}?"):
                print(f"skipped {name}")
                continue
            address = settings.mgmt_address(name)
            with private_file(f"{name}.yaml", configs[name]) as path:
                code = talos.call(address, ["apply-config", "--file", str(path), "--mode", mode])
            if code != 0:
                raise CommandFailed(
                    f"apply failed for node '{name}'",
                    hint=f"talosctl exited with {code}; the remaining nodes were not touched",
                )
            applied.append(name)
    return applied


def apply_insecure(ctx: Context, name: str, *, confirm: Confirm) -> bool:
    """First install of a node booted into maintenance mode (no talosconfig, node's `ip`)."""
    settings, _, configs = _prepare(ctx, [name])
    node = settings.node(name)
    image, disk = install_summary(configs[name])
    print(f"node:      {name} at {node.ip} (maintenance mode)")
    print(f"installer: {image}")
    print(f"disk:      {disk}")
    if not confirm(f"install Talos on {name}? The selected disk will be overwritten"):
        print("aborted, nothing was sent")
        return False
    require_tool("talosctl")
    with private_file(f"{name}.yaml", configs[name]) as path:
        code = ctx.runner.call(["talosctl", "apply-config", "--insecure", "-n", node.ip, "--file", str(path)])
    if code != 0:
        raise CommandFailed(
            f"install failed for node '{name}'",
            hint=f"talosctl exited with {code}; is the node booted into maintenance mode at {node.ip}?",
        )
    print(f"next: once {name} has rebooted, check it with `talkub plan {name}`")
    return True
