"""Schematic intent in .talkub.yaml: resolving IDs and editing extension lists."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from talkub.config import Settings
from talkub.config.schematic import customization_hash
from talkub.core.errors import ValidationError
from talkub.talos.factory import FactoryClient


def used_platforms(settings: Settings) -> list[str]:
    return sorted({settings.platform} | {settings.node_platform(name) for name in settings.nodes})


def refresh(settings: Settings, client: FactoryClient, platforms: Iterable[str]) -> bool:
    """Resolve schematic IDs whose cache is missing or outdated. Returns True if anything changed."""
    changed = False
    for platform in platforms:
        spec = settings.ensure_schematic(platform)
        if spec.is_current:
            continue
        spec.id = client.schematic_id(spec.extensions, spec.kernel_args)
        spec.hash = customization_hash(spec.extensions, spec.kernel_args)
        changed = True
    return changed


def add_extensions(settings: Settings, platform: str, names: Sequence[str], client: FactoryClient) -> list[str]:
    official = set(client.official_extensions(settings.talos_version))
    unknown = sorted(set(names) - official)
    if unknown:
        raise ValidationError(
            f"unknown extension(s) for Talos {settings.talos_version}: {', '.join(unknown)}",
            hint="list them with `talkub image extensions`",
        )
    spec = settings.ensure_schematic(platform)
    added = sorted(set(names) - set(spec.extensions))
    spec.extensions = sorted(set(spec.extensions) | set(names))
    refresh(settings, client, [platform])
    return added


def remove_extensions(settings: Settings, platform: str, names: Sequence[str], client: FactoryClient) -> list[str]:
    spec = settings.ensure_schematic(platform)
    missing = sorted(set(names) - set(spec.extensions))
    if missing:
        raise ValidationError(f"not in the {platform} schematic: {', '.join(missing)}")
    spec.extensions = sorted(set(spec.extensions) - set(names))
    refresh(settings, client, [platform])
    return sorted(set(names))
