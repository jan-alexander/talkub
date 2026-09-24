"""plan / apply: send rendered machine configs to nodes."""

from __future__ import annotations

import os
import sys
import time
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


HEALTH_TIMEOUT = "10m"


def ensure_images(ctx: Context, settings: Settings, order: Sequence[str]) -> None:
    """Every node's platform has a schematic; outdated IDs are resolved and saved."""
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


def prepare(
    ctx: Context, names: Sequence[str], *, refresh: bool = True
) -> tuple[Settings, list[str], dict[str, bytes]]:
    """Load settings, order the nodes and render their configs.

    `refresh=False` skips `ensure_images` entirely: no Image Factory call, no write to
    `.talkub.yaml`. Rendering then uses whatever schematic IDs are already cached, and
    raises `ValidationError` (via `Settings.installer_image`) if one is missing or outdated.
    """
    settings = ctx.repo.load_settings()
    order = ordered_nodes(settings, names)
    if refresh:
        ensure_images(ctx, settings, order)
    return settings, order, render_configs(ctx, settings, order)


def is_bootstrapped(ctx: Context) -> bool:
    return ctx.repo.kubeconfig.is_file()


def health_target(settings: Settings, touched: str) -> str:
    """Management address to run `talosctl health` against after `touched` was changed.

    Workers do not serve the Kubernetes API, so health always goes through a control plane:
    the first one other than `touched`; on a single-CP cluster that CP itself.
    """
    controlplanes = [n for n in ordered_nodes(settings, []) if settings.nodes[n].role == "controlplane"]
    others = [n for n in controlplanes if n != touched]
    if others:
        return settings.mgmt_address(others[0])
    if touched in controlplanes or not controlplanes:
        return settings.mgmt_address(touched)
    return settings.mgmt_address(controlplanes[0])


def apply_settle_seconds() -> float:
    """Pause after `apply-config --mode auto|try` so the node's reboot starts before health is checked."""
    raw = os.environ.get("TALKUB_APPLY_SETTLE_SECONDS", "30")
    try:
        value = float(raw)
    except ValueError:
        value = -1.0
    if not 0 <= value < float("inf"):
        raise ValidationError(f"invalid TALKUB_APPLY_SETTLE_SECONDS {raw!r}", hint="use a number of seconds >= 0")
    return value


def wait_healthy(talos: TalosSession, address: str, *, settle: float = 0.0) -> None:
    if settle > 0:
        print(f"   waiting {settle:g}s for the change to take effect", flush=True)
        time.sleep(settle)
    print(f"   waiting for the cluster to become healthy (via {address})", flush=True)
    code = talos.call(address, ["health", "--wait-timeout", HEALTH_TIMEOUT])
    if code != 0:
        raise CommandFailed(f"{address} did not become healthy", hint=f"talosctl health exited with {code}")


def health_precheck(talos: TalosSession, settings: Settings, *, enabled: bool) -> Callable[[], None]:
    """A callable that checks cluster health once, right before the first disruptive change."""
    done = not enabled

    def check() -> None:
        nonlocal done
        if done:
            return
        done = True
        address = settings.mgmt_address(ordered_nodes(settings, [])[0])  # the first control plane
        try:
            wait_healthy(talos, address)
        except CommandFailed as err:
            raise CommandFailed(
                "the cluster is not healthy; nothing was changed",
                hint=f"{err} ({err.hint}); fix the cluster first (`talkub status`), "
                "or `talkub apply --force-unhealthy` if the pending change is the fix",
            ) from None

    return check


def _dry_run(talos: TalosSession, settings: Settings, name: str, config: bytes, mode: str | None = None) -> None:
    address = settings.mgmt_address(name)
    print(f"== {name} ({address})", flush=True)
    mode_args = ["--mode", mode] if mode else []
    with private_file(f"{name}.yaml", config) as path:
        code = talos.call_redacted(address, ["apply-config", "--file", str(path), "--dry-run", *mode_args])
    if code != 0:
        raise CommandFailed(
            f"dry-run failed for node '{name}'", hint=f"talosctl exited with {code}; is {address} reachable?"
        )


def plan(ctx: Context, names: Sequence[str]) -> None:
    settings, order, configs = prepare(ctx, names)
    with talos_session(ctx) as talos:
        for name in order:
            _dry_run(talos, settings, name, configs[name])


def apply(
    ctx: Context, names: Sequence[str], *, mode: str = "auto", confirm: Confirm, skip_precheck: bool = False
) -> list[str]:
    if mode not in MODES:
        raise ValidationError(f"unknown mode {mode!r}", hint=f"use one of: {', '.join(MODES)}")
    settings, order, configs = prepare(ctx, names)
    rolling = mode in ("auto", "try") and is_bootstrapped(ctx)
    settle = apply_settle_seconds() if rolling else 0.0
    applied: list[str] = []
    with talos_session(ctx) as talos:
        precheck = health_precheck(talos, settings, enabled=rolling and not skip_precheck)
        for index, name in enumerate(order):
            try:
                _apply_one(talos, settings, name, configs[name], mode, confirm, applied, precheck)
                if rolling and applied and applied[-1] == name and index < len(order) - 1:
                    wait_healthy(talos, health_target(settings, name), settle=settle)
            except CommandFailed as err:
                progress = (
                    f"already applied: {', '.join(applied) or 'none'}; "
                    f"not touched: {', '.join(order[index + 1:]) or 'none'}"
                )
                raise CommandFailed(str(err), hint=f"{err.hint}; {progress}") from None
    return applied


def _apply_one(
    talos: TalosSession,
    settings: Settings,
    name: str,
    config: bytes,
    mode: str,
    confirm: Confirm,
    applied: list[str],
    precheck: Callable[[], None],
) -> None:
    _dry_run(talos, settings, name, config, mode)
    if not confirm(f"apply this configuration to {name}?"):
        print(f"skipped {name}")
        return
    precheck()
    address = settings.mgmt_address(name)
    with private_file(f"{name}.yaml", config) as path:
        code = talos.call(address, ["apply-config", "--file", str(path), "--mode", mode])
    if code != 0:
        raise CommandFailed(f"apply failed for node '{name}'", hint=f"talosctl exited with {code}")
    applied.append(name)


def apply_insecure(ctx: Context, name: str, *, confirm: Confirm) -> bool:
    """First install of a node booted into maintenance mode (no talosconfig, node's `ip`)."""
    settings, _, configs = prepare(ctx, [name])
    node = settings.node(name)
    image, disk = install_summary(configs[name])
    if "unknown" in (image, disk):
        raise ValidationError(
            f"cannot determine installer image/install disk from the rendered config for '{name}'",
            hint="refusing to install; check talosctl version / the node patch",
        )
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
