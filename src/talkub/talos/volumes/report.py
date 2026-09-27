"""Plain-text tables for the assistant: the node's disks and the simulated allocation."""

from __future__ import annotations

from talkub.talos.volumes.model import Disk, Inventory
from talkub.talos.volumes.simulate import Plan
from talkub.talos.volumes.sizes import format_bytes


def _content(disk: Disk, inventory: Inventory, system_disk: str | None) -> str:
    notes = [flag for flag in ("readonly", "cdrom") if getattr(disk, flag)]
    if disk.id == system_disk:
        notes.append("system")
    top = next((d for d in inventory.discovered if d.dev_path == disk.dev_path), None)
    parts = [d for d in inventory.discovered if d.type == "partition" and d.parent_dev_path == disk.dev_path]
    if top is not None and top.name:
        described = ", ".join(f"{p.label or p.dev_path}" + (f" ({p.name})" if p.name else "") for p in parts)
        notes.append(f"{top.name}: {described}" if described else top.name)
    elif top is not None:
        notes.append("empty")
    return ", ".join(notes) or "-"


def _columns(rows: list[list[str]]) -> str:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip() for row in rows)


def disk_table(inventory: Inventory, system_disk: str | None = None) -> str:
    rows = [["#", "DEVICE", "SIZE", "TYPE", "TRANSPORT", "MODEL", "SERIAL", "CONTENT"]]
    for index, disk in enumerate(inventory.disks, start=1):
        rows.append(
            [
                str(index),
                disk.dev_path,
                format_bytes(disk.size),
                "HDD" if disk.rotational else "SSD",
                disk.transport or "-",
                disk.model.strip() or "-",
                disk.serial or "-",
                _content(disk, inventory, system_disk),
            ]
        )
    return _columns(rows)


def plan_text(plan: Plan, inventory: Inventory, system_disk: str | None) -> str:
    lines: list[str] = []
    for disk in inventory.disks:
        used = [a for a in plan.allocations if a.disk == disk.id]
        if not used and disk.id not in plan.reserved:
            continue
        cells = []
        if disk.id in plan.reserved:
            cells.append(f"system ≈{plan.reserved[disk.id] / 1024**3:.1f} GiB")
        cells += [f"{a.volume} {format_bytes(a.size)}" + (" (exists)" if a.existing else "") for a in used]
        cells.append(f"free {format_bytes(plan.free.get(disk.id, 0))}")
        star = " ★" if disk.id == system_disk else ""
        lines.append(f"{disk.dev_path}{star} ({format_bytes(disk.size)}): " + " │ ".join(cells))
    lines += [f"✗ {volume}: {message}" for volume, message in plan.errors.items()]
    lines += [f"⚠ {warning}" for warning in plan.warnings]
    if plan.inexact:
        lines.append("note: sizes are approximate (partition offsets unknown)")
    if system_disk is not None:
        lines.append("★ = system disk")
    return "\n".join(lines)
