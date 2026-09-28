"""upgrade talos / upgrade k8s towards the versions in .talkub.yaml."""

from __future__ import annotations

import time
from collections.abc import Sequence
from contextlib import nullcontext

from talkub.config import Settings
from talkub.context import Context
from talkub.core.errors import CommandFailed
from talkub.kube.session import KubeSession, kube_session
from talkub.talos import inspect, tailscale
from talkub.talos.client import TalosSession, talos_session
from talkub.talos.lifecycle import (
    Confirm,
    ensure_images,
    health_precheck,
    health_target,
    is_bootstrapped,
    ordered_nodes,
    wait_healthy,
)

DRAIN_TIMEOUT = "5m"
REBOOT_TIMEOUT = 600.0  # seconds until the rebooted node must report the target version
POLL_INTERVAL = 10.0
PROBE_TIMEOUT = 20.0  # per talosctl call while the node may be down


def _short(value: str | None) -> str:
    return value[:12] if value else "unknown"


def _should_drain(settings: Settings, name: str) -> bool:
    """Whether to drain `name` before its upgrade reboot: only if its pods have another node to go to.

    On a single-node cluster evicted pods would just stay Pending, and a PodDisruptionBudget could
    block the drain until it times out."""
    return len(settings.nodes) > 1


def _drain(kube: KubeSession, name: str) -> None:
    """Cordon and evict `name` through the repo kubeconfig (reachable where talosctl's is not).

    talosctl's own drain uses a kubeconfig fetched from the node, which points at the cluster
    endpoint; behind `talkub tailscale lockdown` that endpoint is firewalled from the workstation."""
    print(f"   draining {name}", flush=True)
    code = kube.kubectl_call(
        [
            "drain",
            name,
            "--ignore-daemonsets",
            "--delete-emptydir-data",
            f"--timeout={DRAIN_TIMEOUT}",
        ]
    )
    if code != 0:
        raise CommandFailed(
            f"drain failed for node '{name}'",
            hint=f"kubectl exited with {code}; the node may be cordoned: `talkub kubectl uncordon {name}`",
        )


def _uncordon(kube: KubeSession, name: str) -> None:
    code = kube.kubectl_call(["uncordon", name])
    if code != 0:
        raise CommandFailed(
            f"uncordon failed for node '{name}'",
            hint=f"kubectl exited with {code}; run `talkub kubectl uncordon {name}`",
        )


def _running(
    session: TalosSession, address: str, name: str, *, timeout: float | None = None
) -> tuple[str | None, str | None]:
    """The Talos version and schematic ID `name` is running."""
    version = session.run(address, ["version"], check=False, timeout=timeout)
    if version.returncode != 0:
        raise CommandFailed(
            f"cannot reach node '{name}' at {address}",
            hint=(version.stderr or b"").decode().strip() or f"exit code {version.returncode}",
        )
    extensions = session.run(address, ["get", "extensions", "-o", "json"], check=False, timeout=timeout)
    if extensions.returncode != 0:
        raise CommandFailed(
            f"cannot read extensions of node '{name}'",
            hint=(extensions.stderr or b"").decode().strip() or f"exit code {extensions.returncode}",
        )
    return inspect.server_version(version.stdout.decode()), inspect.schematic_id(extensions.stdout.decode())


def _wait_rebooted(session: TalosSession, address: str, name: str, version: str, schematic: str) -> None:
    """Poll until `name` runs `version` with `schematic` (after the reboot talkub triggered).

    Not `talosctl reboot --wait`: its event stream breaks when the node stops the service it is
    reached through (e.g. the Tailscale extension), so talosctl fails although the reboot works.
    Unreachable in between is expected; only the state at the end counts."""
    print(f"   waiting for {name} to come back on {version}", flush=True)
    deadline = time.monotonic() + REBOOT_TIMEOUT
    last = "not reachable yet"
    while True:
        try:
            current, running = _running(session, address, name, timeout=PROBE_TIMEOUT)
            if current == version and running == schematic:
                return
            last = f"runs talos {current or 'unknown'}, schematic {_short(running)}"
        except CommandFailed as err:
            last = err.hint or str(err)
        if time.monotonic() >= deadline:
            raise CommandFailed(
                f"node '{name}' did not come back on {version} within {REBOOT_TIMEOUT:g}s",
                hint=f"last seen: {last}; expected schematic {_short(schematic)}; "
                f"check `talkub talosctl -n {address} dmesg`",
            )
        time.sleep(POLL_INTERVAL)


def talos(ctx: Context, names: Sequence[str], *, confirm: Confirm, skip_precheck: bool = False) -> list[str]:
    """Upgrade nodes whose Talos version or schematic differs from the target, one at a time.

    `skip_precheck` skips the health check before the first upgrade: a node whose boot never
    finishes (e.g. an extension without its configuration) is only fixed by the upgrade itself."""
    settings = ctx.repo.load_settings()
    order = ordered_nodes(settings, names)
    ensure_images(ctx, settings, order)
    rolling = is_bootstrapped(ctx)
    upgraded: list[str] = []
    with talos_session(ctx) as session, kube_session(ctx) if rolling else nullcontext() as kube:
        precheck = health_precheck(
            session, settings, enabled=rolling and not skip_precheck, override="talkub upgrade talos --force-unhealthy"
        )
        for index, name in enumerate(order):
            address = settings.mgmt_address(name)
            platform = settings.node_platform(name)
            image = settings.installer_image(platform)
            wanted = settings.schematics[platform].id
            try:
                current, schematic = _running(session, address, name)
                print(
                    f"== {name} ({address}): talos {current or 'unknown'} -> {settings.talos_version}, "
                    f"schematic {_short(schematic)} -> {_short(wanted)}",
                    flush=True,
                )
                if current == settings.talos_version and schematic == wanted:
                    print("   up to date")
                    continue
                if not confirm(f"upgrade {name} to {image}?"):
                    print(f"skipped {name}")
                    continue
                precheck()
                drain = rolling and _should_drain(settings, name)
                if drain:
                    _drain(kube, name)
                cordoned = f"; {name} is still cordoned: `talkub kubectl uncordon {name}`" if drain else ""
                # The upgrade API only installs; the reboot is a separate call. Doing it ourselves
                # (instead of letting `talosctl upgrade` reboot) also skips talosctl's drain, which
                # uses the node's kubeconfig (see `_drain`).
                code = session.call(address, ["upgrade", "--image", image, "--no-reboot", "--wait"])
                if code != 0:
                    raise CommandFailed(
                        f"upgrade failed for node '{name}'",
                        hint=f"talosctl exited with {code}{cordoned}",
                    )
                code = session.call(address, ["reboot", "--wait=false"])
                if code != 0:
                    raise CommandFailed(
                        f"reboot failed for node '{name}' (the new version is installed)",
                        hint=f"talosctl exited with {code}; retry with `talkub talosctl -n {address} reboot`{cordoned}",
                    )
                try:
                    _wait_rebooted(session, address, name, settings.talos_version, wanted)
                except CommandFailed as err:
                    raise CommandFailed(str(err), hint=f"{err.hint}{cordoned}") from None
                upgraded.append(name)
                if rolling and (drain or index < len(order) - 1):
                    # no settle: `_wait_rebooted` already saw the node back on the new version
                    wait_healthy(session, health_target(settings, name))
                if drain:
                    _uncordon(kube, name)
            except CommandFailed as err:
                progress = (
                    f"already upgraded: {', '.join(upgraded) or 'none'}; "
                    f"not touched: {', '.join(order[index + 1 :]) or 'none'}"
                )
                raise CommandFailed(str(err), hint=f"{err.hint}; {progress}") from None
    return upgraded


def k8s(ctx: Context, *, confirm: Confirm) -> bool:
    """Show `talosctl upgrade-k8s --dry-run`, then upgrade Kubernetes to the target version."""
    settings = ctx.repo.load_settings()
    first = ordered_nodes(settings, [])[0]
    address = settings.mgmt_address(first)
    target = ["upgrade-k8s", "--to", settings.kubernetes_version]
    if tailscale.is_locked_down(ctx.repo):
        # talosctl reaches the API through the cluster endpoint, firewalled from here (see `_drain`)
        target += ["--endpoint", tailscale.admin_server(settings)]
    with talos_session(ctx) as session:
        print(
            "   running the Kubernetes upgrade dry-run (output follows when it finishes)",
            flush=True,
        )
        code = session.call_redacted(address, [*target, "--dry-run"])
        if code != 0:
            raise CommandFailed("upgrade-k8s dry-run failed", hint=f"talosctl exited with {code}")
        if not confirm(f"upgrade Kubernetes to {settings.kubernetes_version}?"):
            print("aborted")
            return False
        code = session.call(address, target)
        if code != 0:
            raise CommandFailed("upgrade-k8s failed", hint=f"talosctl exited with {code}")
    return True
