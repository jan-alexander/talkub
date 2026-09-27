"""Value objects for the node volumes assistant."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from talkub.talos.volumes.selector import Selector

KiB = 1024
MiB = KiB**2
GiB = KiB**3
TiB = KiB**4


@dataclass(frozen=True)
class Disk:
    """One `talosctl get disks` entry; `id` is the kernel name (e.g. nvme0n1)."""

    id: str
    dev_path: str
    size: int
    transport: str = ""
    rotational: bool = False
    readonly: bool = False
    cdrom: bool = False
    model: str = ""
    serial: str = ""
    wwid: str = ""


@dataclass(frozen=True)
class Discovered:
    """One `talosctl get discoveredvolumes` entry: a disk or partition and what is on it."""

    dev_path: str
    type: str  # disk | partition | ...
    size: int
    name: str = ""  # detected format: gpt, xfs, luks, ... ("" = nothing found)
    parent_dev_path: str = ""
    offset: int | None = None  # bytes from the disk start; reported since Talos 1.12
    label: str = ""  # partition label, e.g. u-data, EPHEMERAL


@dataclass(frozen=True)
class Inventory:
    disks: tuple[Disk, ...]
    discovered: tuple[Discovered, ...]
    system_disk: str | None  # disk id; None before installation
    installed: bool

    def content(self, disk: Disk) -> tuple[Discovered | None, list[Discovered]]:
        """(the disk's own discovered entry, its partitions) from `discovered`."""
        top = next((d for d in self.discovered if d.dev_path == disk.dev_path), None)
        parts = [d for d in self.discovered if d.type == "partition" and d.parent_dev_path == disk.dev_path]
        return top, parts


class SizeKind(Enum):
    BYTES = "bytes"
    PERCENT = "percent"  # of the largest free region when the volume is created
    NEGATIVE = "negative"  # free region minus value


@dataclass(frozen=True)
class Size:
    kind: SizeKind
    value: int  # bytes, or percent 0-100
    text: str  # as written to the Talos document


class Kind(Enum):
    PARTITION = "partition"  # UserVolumeConfig, partition with filesystem
    RAW = "raw"  # RawVolumeConfig, partition without filesystem
    DISK = "disk"  # UserVolumeConfig volumeType disk, whole disk
    EPHEMERAL = "ephemeral"  # VolumeConfig EPHEMERAL


_PREFIX = {Kind.PARTITION: "u-", Kind.DISK: "u-", Kind.RAW: "r-"}


@dataclass(frozen=True)
class VolumeSpec:
    name: str
    kind: Kind
    selector: Selector | None  # None: Talos default (EPHEMERAL: the system disk)
    min_size: Size | None = None
    max_size: Size | None = None
    grow: bool = False
    filesystem: str = "xfs"
    encrypted: bool = False

    @property
    def volume_id(self) -> str:
        if self.kind is Kind.EPHEMERAL:
            return "EPHEMERAL"
        return _PREFIX[self.kind] + self.name
