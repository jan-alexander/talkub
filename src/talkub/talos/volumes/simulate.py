"""Predict where Talos puts each volume and how large it becomes (spec §2, §7).

Mirrors Talos' volume manager: `CompareVolumeConfigs` ordering, first candidate disk whose
largest contiguous free region holds `minSize`, and `CreatePartition` sizing. Sizes on the install
disk and after partitions without offsets are approximate (`Plan.inexact`).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from talkub.talos.volumes.model import Disk, GiB, Inventory, Kind, MiB, Size, SizeKind, VolumeSpec
from talkub.talos.volumes.selector import is_system
from talkub.talos.volumes.sizes import format_bytes

# EFI/BOOT 2101 MiB (UKI or GRUB layout) + META 1 MiB + STATE 100 MiB (Talos >= 1.11, imager quirks).
SYSTEM_LAYOUT = 2202 * MiB
ALIGN = MiB
GPT_HEAD = MiB
GPT_TAIL = MiB
DEFAULT_MIN = 100 * MiB
EPHEMERAL_MIN = 2 * GiB

DEFAULT_EPHEMERAL = VolumeSpec(
    "EPHEMERAL", Kind.EPHEMERAL, None, min_size=Size(SizeKind.BYTES, EPHEMERAL_MIN, "2GiB"), grow=True
)


@dataclass(frozen=True)
class Allocation:
    volume: str  # volume id
    disk: str  # disk id
    size: int
    existing: bool = False


@dataclass
class Plan:
    allocations: list[Allocation] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)  # volume id -> why it gets no space
    warnings: list[str] = field(default_factory=list)
    free: dict[str, int] = field(default_factory=dict)  # disk id -> free bytes left
    reserved: dict[str, int] = field(default_factory=dict)  # disk id -> Talos system partitions
    inexact: bool = False


@dataclass
class _DiskState:
    regions: list[tuple[int, int]]  # free [start, end)
    format: str = ""  # foreign signature; "" or "gpt" is usable
    blank: bool = True

    def largest(self) -> tuple[int, int] | None:
        return max(self.regions, key=lambda r: r[1] - r[0], default=None)


def _align_up(value: int) -> int:
    return -(-value // ALIGN) * ALIGN


def _align_down(value: int) -> int:
    return value // ALIGN * ALIGN


def _gaps(start: int, end: int, used: list[tuple[int, int]]) -> list[tuple[int, int]]:
    regions: list[tuple[int, int]] = []
    cursor = start
    for used_start, used_end in sorted(used):
        if _align_down(used_start) - cursor >= ALIGN:
            regions.append((cursor, _align_down(used_start)))
        cursor = max(cursor, _align_up(used_end))
    if end - cursor >= ALIGN:
        regions.append((cursor, end))
    return regions


def _initial(inventory: Inventory, install_disk: str | None, plan: Plan) -> dict[str, _DiskState]:
    states: dict[str, _DiskState] = {}
    if inventory.disks and not inventory.discovered:
        plan.inexact = True  # partitions unknown: every disk looks empty
    for disk in inventory.disks:
        start, end = GPT_HEAD, _align_down(disk.size - GPT_TAIL)
        if disk.id == install_disk:
            plan.reserved[disk.id] = SYSTEM_LAYOUT
            states[disk.id] = _DiskState([(start + SYSTEM_LAYOUT, end)], blank=False)
            continue
        top = next((d for d in inventory.discovered if d.dev_path == disk.dev_path), None)
        fmt = top.name if top is not None else ""
        parts = [d for d in inventory.discovered if d.type == "partition" and d.parent_dev_path == disk.dev_path]
        if fmt and fmt != "gpt":
            states[disk.id] = _DiskState([], format=fmt, blank=False)
            continue
        if any(p.offset is None for p in parts):
            plan.inexact = True
            used, cursor = [], start
            for part in parts:
                used.append((cursor, cursor + part.size))
                cursor = _align_up(cursor + part.size)
        else:
            used = [(p.offset or 0, (p.offset or 0) + p.size) for p in parts]
        states[disk.id] = _DiskState(_gaps(start, end, used), blank=not fmt and not parts)
    return states


def _order(volume: VolumeSpec) -> tuple[int, bool, float, str]:
    wave = -1 if volume.kind is Kind.EPHEMERAL else 0
    maximum = volume.max_size.value if volume.max_size and volume.max_size.kind is SizeKind.BYTES else math.inf
    return wave, volume.grow, maximum, volume.volume_id


def _existing(inventory: Inventory, volume: VolumeSpec) -> Allocation | None:
    for part in inventory.discovered:
        if part.label and part.label == volume.volume_id:
            disk = next((d for d in inventory.disks if d.dev_path == part.parent_dev_path), None)
            return Allocation(volume.volume_id, disk.id if disk else part.parent_dev_path, part.size, existing=True)
    return None


def _candidates(inventory: Inventory, volume: VolumeSpec, system_disk: str | None) -> list[Disk]:
    usable = [d for d in inventory.disks if not d.readonly]
    if volume.selector is None:  # EPHEMERAL default: the system disk
        return [d for d in usable if d.id == system_disk]
    return [d for d in usable if volume.selector.matches(d, is_system(d, system_disk, install=False))]


def _size(maximum: Size | None, available: int) -> int:
    if maximum is None:
        return available
    if maximum.kind is SizeKind.PERCENT:
        return available * maximum.value // 100
    if maximum.kind is SizeKind.NEGATIVE:
        return available - maximum.value
    return min(maximum.value, available)


def simulate(
    inventory: Inventory,
    volumes: Sequence[VolumeSpec],
    *,
    system_disk: str | None,
    install_disk: str | None,
) -> Plan:
    plan = Plan()
    states = _initial(inventory, install_disk, plan)
    ephemeral_unlimited = any(v.kind is Kind.EPHEMERAL and v.max_size is None for v in volumes)
    for volume in sorted(volumes, key=_order):
        vid = volume.volume_id
        found = _existing(inventory, volume) if inventory.installed else None
        if found is not None:
            plan.allocations.append(found)
            continue
        if volume.selector is not None and not volume.selector.verifiable:
            plan.warnings.append(f"{vid}: custom selector, not simulated")
            continue
        candidates = _candidates(inventory, volume, system_disk)
        if not candidates:
            plan.errors[vid] = "selector matches no disk" if volume.selector else "system disk unknown"
            continue
        if volume.kind is Kind.DISK:
            if len(candidates) > 1:
                plan.errors[vid] = f"whole-disk selector matches several disks: {', '.join(d.id for d in candidates)}"
                continue
            disk, state = candidates[0], states[candidates[0].id]
            if not state.blank:
                plan.errors[vid] = f"disk {disk.id} is not empty"
                continue
            state.regions, state.blank = [], False
            plan.allocations.append(Allocation(vid, disk.id, disk.size))
            continue
        if len(candidates) > 1:
            names = ", ".join(d.id for d in candidates)
            plan.warnings.append(f"{vid}: selector matches {names}; Talos takes the first with enough space")
        need = (
            volume.min_size.value
            if volume.min_size
            else (EPHEMERAL_MIN if volume.kind is Kind.EPHEMERAL else DEFAULT_MIN)
        )
        reasons: list[str] = []
        for disk in candidates:
            state = states[disk.id]
            if state.format:
                reasons.append(f"{disk.id}: not GPT ({state.format})")
                continue
            region = state.largest()
            available = region[1] - region[0] if region else 0
            if region is None or available < need:
                hint = " (EPHEMERAL takes the rest of the system disk; limit EPHEMERAL)"
                extra = hint if disk.id == system_disk and ephemeral_unlimited else ""
                reasons.append(f"{disk.id}: not enough space ({format_bytes(available)} free){extra}")
                continue
            size = _align_down(_size(volume.max_size, available))
            if size <= 0:
                reasons.append(f"{disk.id}: size would be {format_bytes(max(size, 0))}")
                continue
            state.regions.remove(region)
            if region[1] - (region[0] + size) >= ALIGN:
                state.regions.append((region[0] + size, region[1]))
            state.blank = False
            plan.allocations.append(Allocation(vid, disk.id, size))
            break
        else:
            plan.errors[vid] = "; ".join(reasons)
    plan.free = {disk_id: sum(end - start for start, end in s.regions) for disk_id, s in states.items()}
    return plan
