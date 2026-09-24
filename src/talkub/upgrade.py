"""upgrade talos / upgrade k8s towards the versions in .talkub.yaml."""

from __future__ import annotations

from collections.abc import Sequence

from talkub.context import Context
from talkub.errors import CommandFailed
from talkub.lifecycle import (
    Confirm,
    ensure_images,
    health_precheck,
    health_target,
    is_bootstrapped,
    ordered_nodes,
    wait_healthy,
)
from talkub.talos import inspect
from talkub.talos.client import talos_session


def _short(value: str | None) -> str:
    return value[:12] if value else "unknown"


def talos(ctx: Context, names: Sequence[str], *, confirm: Confirm) -> list[str]:
    """Upgrade nodes whose Talos version or schematic differs from the target, one at a time."""
    settings = ctx.repo.load_settings()
    order = ordered_nodes(settings, names)
    ensure_images(ctx, settings, order)
    rolling = is_bootstrapped(ctx)
    upgraded: list[str] = []
    with talos_session(ctx) as session:
        precheck = health_precheck(session, settings, enabled=rolling)
        for index, name in enumerate(order):
            address = settings.mgmt_address(name)
            platform = settings.node_platform(name)
            image = settings.installer_image(platform)
            wanted = settings.schematics[platform].id
            try:
                version = session.run(address, ["version"], check=False)
                if version.returncode != 0:
                    raise CommandFailed(
                        f"cannot reach node '{name}' at {address}",
                        hint=(version.stderr or b"").decode().strip() or f"exit code {version.returncode}",
                    )
                current = inspect.server_version(version.stdout.decode())
                extensions = session.run(address, ["get", "extensions", "-o", "json"], check=False)
                if extensions.returncode != 0:
                    raise CommandFailed(
                        f"cannot read extensions of node '{name}'",
                        hint=(extensions.stderr or b"").decode().strip() or f"exit code {extensions.returncode}",
                    )
                schematic = inspect.schematic_id(extensions.stdout.decode())
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
                code = session.call(address, ["upgrade", "--image", image, "--wait"])
                if code != 0:
                    raise CommandFailed(f"upgrade failed for node '{name}'", hint=f"talosctl exited with {code}")
                upgraded.append(name)
                if rolling and index < len(order) - 1:
                    # no settle: `upgrade --wait` already waited for the reboot
                    wait_healthy(session, health_target(settings, name))
            except CommandFailed as err:
                progress = (
                    f"already upgraded: {', '.join(upgraded) or 'none'}; "
                    f"not touched: {', '.join(order[index + 1:]) or 'none'}"
                )
                raise CommandFailed(str(err), hint=f"{err.hint}; {progress}") from None
    return upgraded


def k8s(ctx: Context, *, confirm: Confirm) -> bool:
    """Show `talosctl upgrade-k8s --dry-run`, then upgrade Kubernetes to the target version."""
    settings = ctx.repo.load_settings()
    first = ordered_nodes(settings, [])[0]
    address = settings.mgmt_address(first)
    target = ["upgrade-k8s", "--to", settings.kubernetes_version]
    with talos_session(ctx) as session:
        print("   running the Kubernetes upgrade dry-run (output follows when it finishes)", flush=True)
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
