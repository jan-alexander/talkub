"""image extensions / add / remove / show."""

from __future__ import annotations

import typer

from talkub import images
from talkub.errors import ValidationError
from talkub.factory import FactoryClient, boot_media_url
from talkub.repo import ARCHES, PLATFORMS

app = typer.Typer(
    help="Manage Image Factory schematics (system extensions).", no_args_is_help=True
)

PLATFORM = typer.Option(
    None, "--platform", help="metal or hcloud (default: the cluster platform)."
)


def _check_platform(platform: str | None) -> None:
    if platform is not None and platform not in PLATFORMS:
        raise ValidationError(
            f"unknown platform {platform!r}", hint=f"use one of: {', '.join(PLATFORMS)}"
        )


@app.command("extensions")
def extensions(ctx: typer.Context, platform: str = PLATFORM) -> None:
    """List official extensions for the target Talos version (* = active)."""
    _check_platform(platform)
    settings = ctx.obj.repo().load_settings()
    active = set(settings.ensure_schematic(platform or settings.platform).extensions)
    for name in FactoryClient().official_extensions(settings.talos_version):
        print(f"{'*' if name in active else ' '} {name}")


@app.command("add")
def add(
    ctx: typer.Context,
    names: list[str] = typer.Argument(
        ..., help="Extensions, e.g. siderolabs/tailscale."
    ),
    platform: str = PLATFORM,
) -> None:
    """Add extensions to a platform's schematic."""
    _check_platform(platform)
    repo = ctx.obj.repo()
    settings = repo.load_settings()
    target = platform or settings.platform
    added = images.add_extensions(settings, target, names, FactoryClient())
    repo.write_settings(settings)
    print(
        f"{target}: added {', '.join(added) or 'nothing'}; schematic {settings.schematics[target].id}"
    )
    print("roll the new image out to running nodes with `talkub upgrade talos`")


@app.command("remove")
def remove(
    ctx: typer.Context,
    names: list[str] = typer.Argument(..., help="Extensions to remove."),
    platform: str = PLATFORM,
) -> None:
    """Remove extensions from a platform's schematic."""
    _check_platform(platform)
    repo = ctx.obj.repo()
    settings = repo.load_settings()
    target = platform or settings.platform
    removed = images.remove_extensions(settings, target, names, FactoryClient())
    repo.write_settings(settings)
    print(
        f"{target}: removed {', '.join(removed)}; schematic {settings.schematics[target].id}"
    )
    print("roll the new image out to running nodes with `talkub upgrade talos`")


@app.command("show")
def show(
    ctx: typer.Context,
    platform: str = PLATFORM,
    arch: str = typer.Option(
        "amd64", "--arch", help="amd64 or arm64 (for the boot medium URL)."
    ),
) -> None:
    """Show the installer image and the boot medium download URL."""
    _check_platform(platform)
    if arch not in ARCHES:
        raise ValidationError(
            f"unknown arch {arch!r}", hint=f"use one of: {', '.join(ARCHES)}"
        )
    repo = ctx.obj.repo()
    settings = repo.load_settings()
    target = platform or settings.platform
    if images.refresh(settings, FactoryClient(), [target]):
        repo.write_settings(settings)
    print(f"installer:  {settings.installer_image(target)}")
    print(
        f"boot media: {boot_media_url(settings.schematics[target].id, settings.talos_version, target, arch)}"
    )
