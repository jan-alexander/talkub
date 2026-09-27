"""Rules Talos enforces on volume documents, checked before anything is written (spec §2)."""

from __future__ import annotations

import re

from talkub.core.errors import ValidationError
from talkub.talos.volumes.model import Kind, SizeKind, VolumeSpec

NAME = re.compile(r"[A-Za-z0-9-]{1,34}")
FILESYSTEMS = ("xfs", "ext4", "btrfs")
_SINCE = {
    "volumes": (1, 11),
    "disk": (1, 12),
    "percent": (1, 12),
    "negative": (1, 13),
    "install": (1, 14),
    "btrfs": (1, 14),
}
_VERSION = re.compile(r"v?(\d+)\.(\d+)")


def version_tuple(text: str) -> tuple[int, int]:
    match = _VERSION.match(text.strip())
    if match is None:
        raise ValidationError(f"cannot read Talos version {text!r}", hint="talosVersion in .talkub.yaml, e.g. v1.14.0")
    return int(match[1]), int(match[2])


def supports(talos_version: str, feature: str) -> bool:
    return version_tuple(talos_version) >= _SINCE[feature]


def _need(talos_version: str, feature: str, what: str) -> None:
    if not supports(talos_version, feature):
        major, minor = _SINCE[feature]
        raise ValidationError(f"{what} needs Talos v{major}.{minor} or newer (talosVersion is {talos_version})")


def check_volume(spec: VolumeSpec, talos_version: str) -> None:
    _need(talos_version, "volumes", "user volumes")
    if spec.kind is not Kind.EPHEMERAL and not NAME.fullmatch(spec.name):
        raise ValidationError(
            f"invalid volume name {spec.name!r}",
            hint="1 to 34 characters: letters, digits and '-'",
        )
    if spec.kind is Kind.DISK:
        _need(talos_version, "disk", "a whole-disk volume")
        if spec.min_size is not None or spec.max_size is not None or spec.grow:
            raise ValidationError("a whole-disk volume takes no min size, max size or grow")
    elif spec.min_size is None and spec.max_size is None:
        raise ValidationError(f"volume '{spec.name}': min size or max size is required")
    if spec.min_size is not None and spec.min_size.kind is not SizeKind.BYTES:
        raise ValidationError(f"volume '{spec.name}': min size must be an absolute size")
    if spec.max_size is not None:
        if spec.max_size.kind is SizeKind.PERCENT:
            _need(talos_version, "percent", "a percentage max size")
        if spec.max_size.kind is SizeKind.NEGATIVE:
            _need(talos_version, "negative", "a negative max size")
            if spec.kind is Kind.RAW:
                raise ValidationError("a raw volume cannot use a negative max size (Talos treats it as positive)")
        if spec.grow and spec.max_size.kind is not SizeKind.BYTES:
            raise ValidationError("grow needs an absolute max size (otherwise the volume grows without limit)")
        if (
            spec.min_size is not None
            and spec.max_size.kind is SizeKind.BYTES
            and spec.min_size.value > spec.max_size.value
        ):
            raise ValidationError(f"volume '{spec.name}': min size is larger than max size")
    if spec.kind in (Kind.PARTITION, Kind.DISK):
        if spec.filesystem not in FILESYSTEMS:
            raise ValidationError(f"unsupported filesystem {spec.filesystem!r}", hint=", ".join(FILESYSTEMS))
        if spec.filesystem == "btrfs":
            _need(talos_version, "btrfs", "btrfs")
