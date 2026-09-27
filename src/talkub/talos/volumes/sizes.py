"""Sizes as Talos parses them (go-humanize) and the size intents offered by the assistant."""

from __future__ import annotations

import re
from enum import Enum

from talkub.core.errors import ValidationError
from talkub.talos.volumes.model import GiB, KiB, MiB, Size, SizeKind, TiB

_UNITS = {
    "": 1,
    "b": 1,
    "kb": 1000,
    "mb": 1000**2,
    "gb": 1000**3,
    "tb": 1000**4,
    "kib": KiB,
    "mib": MiB,
    "gib": GiB,
    "tib": TiB,
}
_BYTES = re.compile(r"(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>[A-Za-z]*)")
_HINT = "e.g. 100GiB (binary) or 100GB (decimal)"

REST_MIN = Size(SizeKind.BYTES, GiB, "1GiB")
SMALL_MIN = Size(SizeKind.BYTES, 100 * MiB, "100MiB")


def parse_bytes(text: str) -> int:
    match = _BYTES.fullmatch(text.strip())
    if match is None or match.group("unit").lower() not in _UNITS:
        raise ValidationError(f"invalid size {text!r}", hint=_HINT)
    return int(float(match.group("number")) * _UNITS[match.group("unit").lower()])


def _canonical(text: str) -> str:
    return re.sub(r"\s+", "", text.strip())


def parse_size(text: str) -> Size:
    """`100GiB`, `50%` or `-10GiB`."""
    stripped = _canonical(text)
    if stripped.endswith("%"):
        try:
            percent = int(float(stripped[:-1]))
        except ValueError:
            raise ValidationError(f"invalid percentage {text!r}", hint="e.g. 50%") from None
        if not 0 <= percent <= 100 or stripped.startswith("-"):
            raise ValidationError(f"percentage out of range: {text!r}", hint="0% to 100%")
        return Size(SizeKind.PERCENT, percent, f"{percent}%")
    if stripped.startswith("-"):
        return Size(SizeKind.NEGATIVE, parse_bytes(stripped[1:]), stripped)
    return Size(SizeKind.BYTES, parse_bytes(stripped), stripped)


def format_bytes(value: int) -> str:
    for unit, name in ((TiB, "TiB"), (GiB, "GiB"), (MiB, "MiB"), (KiB, "KiB")):
        if value >= unit:
            return f"{value / unit:.1f} {name}"
    return f"{value} B"


class Intent(Enum):
    FIXED = "fixed"
    REST = "rest"
    PERCENT = "percent"
    ALL_BUT = "all-but"


_AMOUNT = {Intent.FIXED: SizeKind.BYTES, Intent.PERCENT: SizeKind.PERCENT, Intent.ALL_BUT: SizeKind.NEGATIVE}


def to_fields(intent: Intent, amount: Size | None, *, grow: bool = False) -> tuple[Size | None, Size | None, bool]:
    """(minSize, maxSize, grow) for a size intent (spec §5).

    Fixed uses min = max so a volume never lands shrunk on a disk with just `minSize` free; percent
    and negative sizes never grow, because Talos only caps growth at an absolute `maxSize`.
    """
    if intent is Intent.REST:
        return REST_MIN, None, grow
    if amount is None or amount.kind is not _AMOUNT[intent]:
        raise ValidationError(f"size {amount.text if amount else None!r} does not fit '{intent.value}'")
    if intent is Intent.FIXED:
        return amount, amount, False
    return SMALL_MIN, amount, False
