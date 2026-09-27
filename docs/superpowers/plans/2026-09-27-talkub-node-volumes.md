# `node volumes` Assistant Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `talkub node volumes <name>` reads a node's disks and interactively writes a verified install-disk selector, an optional EPHEMERAL limit and user/raw/disk volumes into `talos/nodes/<name>.yaml`, with a simulation of Talos' allocation.

**Architecture:** Pure logic in a new package `talkub.talos.volumes` (model, sizes, selector, validate, simulate, render, report; no I/O). Parsing talosctl JSON lives in `talos/inspect.py` (as all talosctl format knowledge does), reading from the node in `talos/disks.py`, patch merging in `talos/nodes.py`, the dialog in `cli/volumes.py`.

**Tech Stack:** Python 3.12, typer (`typer.prompt`/`typer.confirm`), ruamel.yaml via `talkub.core.yamlio`, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-27-talkub-node-volumes-design.md` (German). Read §2 before touching `simulate.py` or `selector.py`: it is the verified Talos semantics this plan encodes.

## Global Constraints

- Code, comments, docstrings, CLI output and error messages in English.
- No new runtime dependency; `typer.prompt`/`typer.confirm` for the dialog.
- Run `uv run ruff format` on touched files before each commit (test code in this plan is not pre-formatted).
- Line length 120; `ruff check`, `ruff format --check`, `lint-imports` must pass (pre-commit hook runs them). `talos` may import `config`, `core`, `context`, `secrets`, `kube`, `argocd`; never `cli`.
- Test file basenames must be unique across `tests/` (no `__init__.py`, `pythonpath = ["tests"]`).
- CEL size literals always carry the `u` suffix: `100u * GiB`.
- Default exclusions on every generated selector: `!disk.readonly && !disk.cdrom && disk.transport != "usb"`; the USB term is omitted when the chosen disk is USB (warn) or when the selector already pins `disk.transport == "<t>"`.
- Install selectors never contain `system_disk` (Talos binds it to `false` during install).
- Volume names: 1–34 characters `[A-Za-z0-9-]`. Volume IDs: `u-<name>` (UserVolumeConfig), `r-<name>` (RawVolumeConfig), `EPHEMERAL`.
- Version floors: volumes ≥ v1.11; `volumeType: disk` and `%` sizes ≥ v1.12; negative sizes ≥ v1.13; `UnattendedInstallConfig` and `btrfs` ≥ v1.14. Negative `maxSize` never on RawVolumeConfig.
- Size intents: fixed X → `minSize: X, maxSize: X`; rest → `minSize: 1GiB`, no `maxSize`, optional `grow`; percent → `minSize: 100MiB, maxSize: "N%"`, `grow: false`; all-but → `minSize: 100MiB, maxSize: "-X"`, `grow: false`. EPHEMERAL documents always write `grow: false`.
- Encryption offered: only `luks2` with one key `slot: 0, nodeID: {}, lockToState: true`.
- Talos system layout on the install disk: 2202 MiB (EFI/BOOT 2101 MiB + META 1 MiB + STATE 100 MiB, Talos ≥ 1.11), 1 MiB alignment, 1 MiB GPT head and tail.

## Review Focus

1. A disk whose `model`/`serial` contains `"` or `\` → the generated CEL must stay valid and `parse()` must round-trip it (Task 2).
2. A selector that matches several disks for a `partition` volume where the first disk is full → Talos (and the simulation) falls through to the next disk; the dialog must warn, not error (Task 5).
3. Re-running the assistant on a patch that already has volume documents → existing volumes are simulated and not duplicated; a name collision is rejected (Task 9).
4. The patch header comment and hand-written documents (HostnameConfig, LinkConfig, …) must survive `upsert_documents` byte-for-byte in content (Task 6).
5. `talosctl get disks` output with fields omitted (`rotational` false, no `serial`) or a trailing garbage document → no crash, defaults apply (Task 4).

---

### Task 1: Model and sizes

**Files:**
- Create: `src/talkub/talos/volumes/__init__.py`
- Create: `src/talkub/talos/volumes/model.py`
- Create: `src/talkub/talos/volumes/sizes.py`
- Test: `tests/talos/volumes/test_volume_sizes.py`

**Interfaces:**
- Produces:
  - `model.KiB, MiB, GiB, TiB: int`
  - `model.Disk(id, dev_path, size, transport="", rotational=False, readonly=False, cdrom=False, model="", serial="", wwid="")` (frozen)
  - `model.Discovered(dev_path, type, size, name="", parent_dev_path="", offset: int | None = None, label="")` (frozen)
  - `model.Inventory(disks: tuple[Disk, ...], discovered: tuple[Discovered, ...], system_disk: str | None, installed: bool)` (frozen)
  - `model.SizeKind` enum `BYTES | PERCENT | NEGATIVE`; `model.Size(kind, value: int, text: str)` (frozen)
  - `model.Kind` enum `PARTITION | RAW | DISK | EPHEMERAL`
  - `model.VolumeSpec(name, kind, selector: Selector | None, min_size: Size | None = None, max_size: Size | None = None, grow=False, filesystem="xfs", encrypted=False)` (frozen) with property `volume_id -> str`
  - `sizes.parse_bytes(text) -> int`, `sizes.parse_size(text) -> Size`, `sizes.format_bytes(n) -> str`, `sizes.Intent` enum `FIXED | REST | PERCENT | ALL_BUT`, `sizes.to_fields(intent, amount: Size | None, *, grow=False) -> tuple[Size | None, Size | None, bool]`, constants `sizes.REST_MIN`, `sizes.SMALL_MIN`

- [ ] **Step 1: Write the failing tests**

`tests/talos/volumes/test_volume_sizes.py`:

```python
import pytest

from talkub.core.errors import ValidationError
from talkub.talos.volumes.model import GiB, MiB, Kind, Size, SizeKind, VolumeSpec
from talkub.talos.volumes.sizes import Intent, format_bytes, parse_bytes, parse_size, to_fields


@pytest.mark.parametrize(
    "text, expected",
    [
        ("100GB", 100 * 1000**3),
        ("100 GiB", 100 * GiB),
        ("1.5GiB", int(1.5 * GiB)),
        ("512mib", 512 * MiB),
        ("4096", 4096),
        ("2TB", 2 * 1000**4),
    ],
)
def test_parse_bytes(text, expected):
    assert parse_bytes(text) == expected


@pytest.mark.parametrize("text", ["", "GB", "10 parsecs", "-5GB", "1e3GB"])
def test_parse_bytes_rejects(text):
    with pytest.raises(ValidationError):
        parse_bytes(text)


def test_parse_size_kinds():
    assert parse_size("100GiB") == Size(SizeKind.BYTES, 100 * GiB, "100GiB")
    assert parse_size(" 50 % ") == Size(SizeKind.PERCENT, 50, "50%")
    assert parse_size("33.9%") == Size(SizeKind.PERCENT, 33, "33%")
    assert parse_size("-10 GiB") == Size(SizeKind.NEGATIVE, 10 * GiB, "-10GiB")


@pytest.mark.parametrize("text", ["101%", "-1%", "abc%"])
def test_parse_size_rejects_bad_percent(text):
    with pytest.raises(ValidationError):
        parse_size(text)


def test_format_bytes():
    assert format_bytes(100 * 1000**3) == "93.1 GiB"
    assert format_bytes(512 * MiB) == "512.0 MiB"
    assert format_bytes(3 * 1024**4) == "3.0 TiB"
    assert format_bytes(10) == "10 B"


def test_to_fields_fixed_sets_min_equal_max():
    size = parse_size("100GiB")
    assert to_fields(Intent.FIXED, size) == (size, size, False)


def test_to_fields_rest_keeps_grow_and_has_no_max():
    min_size, max_size, grow = to_fields(Intent.REST, None, grow=True)
    assert min_size == Size(SizeKind.BYTES, GiB, "1GiB")
    assert max_size is None and grow is True


def test_to_fields_percent_and_all_but_never_grow():
    percent = parse_size("50%")
    assert to_fields(Intent.PERCENT, percent, grow=True) == (Size(SizeKind.BYTES, 100 * MiB, "100MiB"), percent, False)
    negative = parse_size("-10GiB")
    assert to_fields(Intent.ALL_BUT, negative)[1] == negative


def test_to_fields_rejects_wrong_amount_kind():
    with pytest.raises(ValidationError):
        to_fields(Intent.FIXED, parse_size("50%"))
    with pytest.raises(ValidationError):
        to_fields(Intent.PERCENT, parse_size("50GiB"))


def test_volume_ids():
    assert VolumeSpec("data", Kind.PARTITION, None).volume_id == "u-data"
    assert VolumeSpec("data", Kind.DISK, None).volume_id == "u-data"
    assert VolumeSpec("ceph", Kind.RAW, None).volume_id == "r-ceph"
    assert VolumeSpec("EPHEMERAL", Kind.EPHEMERAL, None).volume_id == "EPHEMERAL"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/talos/volumes/test_volume_sizes.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'talkub.talos.volumes'`

- [ ] **Step 3: Implement**

`src/talkub/talos/volumes/__init__.py`:

```python
"""Pure logic of the `node volumes` assistant: disks, selectors, sizes, simulation, documents."""
```

`src/talkub/talos/volumes/model.py`:

```python
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
```

`src/talkub/talos/volumes/sizes.py`:

```python
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
```

`tests/talos/volumes/` needs no `__init__.py`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/talos/volumes/test_volume_sizes.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/talkub/talos/volumes tests/talos/volumes/test_volume_sizes.py
git commit -m "feat(volumes): model and size parsing for the node volumes assistant"
```

---

### Task 2: Selectors

**Files:**
- Create: `src/talkub/talos/volumes/selector.py`
- Test: `tests/talos/volumes/test_selector.py`

**Interfaces:**
- Consumes: `model.Disk`, `model.GiB`
- Produces:
  - `Equals(field, value, negate=False)`, `Flag(field, expected)`, `SystemDisk(expected)`, `SizeRange(low_gib, high_gib)` — each with `cel() -> str` and `test(disk, system: bool) -> bool`
  - `Selector(terms: tuple[Predicate, ...] = (), raw: str | None = None)` with `verifiable: bool`, `to_cel() -> str`, `matches(disk, system: bool) -> bool` (raises `ValueError` when `raw` is set)
  - `parse(text) -> Selector` (round-trips every generated selector; anything else → `Selector(raw=text)`)
  - `suggest(disks, target, system_disk: str | None, *, install: bool) -> list[Selector]` (all unique stages, most portable first; empty if none)
  - `is_system(disk, system_disk, *, install) -> bool`

- [ ] **Step 1: Write the failing tests**

`tests/talos/volumes/test_selector.py`:

```python
import pytest

from talkub.talos.volumes.model import GiB, Disk
from talkub.talos.volumes.selector import Equals, Flag, Selector, SizeRange, SystemDisk, parse, suggest

NVME = Disk("nvme0n1", "/dev/nvme0n1", 512 * GiB, transport="nvme", model="Samsung 990", serial="S1")
NVME2 = Disk("nvme1n1", "/dev/nvme1n1", 2048 * GiB, transport="nvme", model="WD SN850", serial="W1")
SSD = Disk("sda", "/dev/sda", 1024 * GiB, transport="sata", model="Crucial", serial="C1")
HDD = Disk("sdb", "/dev/sdb", 4096 * GiB, transport="sata", rotational=True, model="WD Red", serial="R1")
STICK = Disk("sdc", "/dev/sdc", 32 * GiB, transport="usb", model="Cruzer", serial="U1")
TWIN_A = Disk("sdd", "/dev/sdd", 1024 * GiB, transport="sata", model="Same", serial="A")
TWIN_B = Disk("sde", "/dev/sde", 1024 * GiB, transport="sata", model="Same", serial="B")


def _matching(selector, disks, system_disk=None, install=False):
    return [d.id for d in disks if selector.matches(d, (not install) and d.id == system_disk)]


def test_to_cel_uses_uint_suffix_and_quotes():
    selector = Selector((Flag("readonly", False), Equals("transport", "usb", negate=True), SizeRange(90, 110)))
    assert selector.to_cel() == (
        '!disk.readonly && disk.transport != "usb" && disk.size >= 90u * GiB && disk.size <= 110u * GiB'
    )
    assert Equals("model", 'a"b\\c').cel() == 'disk.model == "a\\"b\\\\c"'


def test_install_prefers_transport_and_excludes_readonly_cdrom():
    options = suggest([NVME, SSD, HDD, STICK], NVME, None, install=True)
    assert options[0].to_cel() == '!disk.readonly && !disk.cdrom && disk.transport == "nvme"'
    assert "system_disk" not in " ".join(o.to_cel() for o in options)
    assert all(_matching(o, [NVME, SSD, HDD, STICK], install=True) == ["nvme0n1"] for o in options)


def test_rotational_stage_distinguishes_ssd_from_hdd():
    first = suggest([NVME, SSD, HDD], SSD, None, install=True)[0]
    assert first.to_cel() == '!disk.readonly && !disk.cdrom && disk.transport == "sata" && !disk.rotational'


def test_size_range_then_model_then_serial():
    first = suggest([NVME, NVME2], NVME2, None, install=True)[0]
    assert first.terms[-1] == SizeRange(1843, 2253)
    twins = suggest([TWIN_A, TWIN_B], TWIN_B, None, install=True)
    assert twins[0].to_cel().endswith('disk.serial == "B"')


def test_usb_target_drops_usb_exclusion():
    first = suggest([NVME, STICK], STICK, None, install=True)[0]
    assert first.to_cel() == '!disk.readonly && !disk.cdrom && disk.transport == "usb"'


def test_serial_stage_keeps_usb_exclusion():
    options = suggest([TWIN_A, TWIN_B], TWIN_A, None, install=True)
    assert options[0].to_cel() == '!disk.readonly && !disk.cdrom && disk.transport != "usb" && disk.serial == "A"'


def test_volume_selectors_pin_system_disk():
    first = suggest([NVME, NVME2], NVME2, "nvme0n1", install=False)[0]
    assert first.to_cel() == '!disk.readonly && !disk.cdrom && disk.transport == "nvme" && !system_disk'
    assert _matching(first, [NVME, NVME2], "nvme0n1") == ["nvme1n1"]
    on_system = suggest([NVME, NVME2], NVME, "nvme0n1", install=False)[0]
    assert on_system.terms[-1] == SystemDisk(True)


def test_no_unique_stage_returns_empty():
    clone = Disk("sdf", "/dev/sdf", TWIN_A.size, transport="sata", model="Same")
    clone2 = Disk("sdg", "/dev/sdg", TWIN_A.size, transport="sata", model="Same")
    assert suggest([clone, clone2], clone, None, install=True) == []


def test_readonly_and_cdrom_never_match():
    ro = Disk("sr0", "/dev/sr0", GiB, transport="sata", readonly=True, cdrom=True)
    assert _matching(suggest([ro, SSD], SSD, None, install=True)[0], [ro, SSD], install=True) == ["sda"]


@pytest.mark.parametrize("disk", [NVME, SSD, HDD, TWIN_A, Disk("x", "/dev/x", GiB, model='we"ird\\')])
def test_parse_round_trips_generated_selectors(disk):
    for option in suggest([disk, NVME2, STICK], disk, "nvme1n1", install=False):
        assert parse(option.to_cel()) == option


@pytest.mark.parametrize("text", ['disk.size > 100u * GB', 'glob("/dev/sd*", disk.dev_path)', "", "disk.readonlyX"])
def test_parse_keeps_foreign_expressions_raw(text):
    selector = parse(text)
    assert selector.raw == text and not selector.verifiable
    assert selector.to_cel() == text
    with pytest.raises(ValueError):
        selector.matches(NVME, False)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/talos/volumes/test_selector.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'talkub.talos.volumes.selector'`

- [ ] **Step 3: Implement**

`src/talkub/talos/volumes/selector.py`:

```python
"""CEL disk selectors the assistant can both write and evaluate.

A selector is an AND of simple predicates over Talos' `disk` object and `system_disk`
(`celenv.DiskLocator`). `to_cel()` writes the expression Talos evaluates; `matches()` evaluates the
same predicates in Python. Expressions talkub did not generate are kept verbatim as `raw` and
cannot be evaluated.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from talkub.talos.volumes.model import GiB, Disk


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unquote(value: str) -> str:
    return re.sub(r"\\(.)", r"\1", value)


@dataclass(frozen=True)
class Equals:
    field: str  # transport | model | serial | wwid
    value: str
    negate: bool = False

    def cel(self) -> str:
        return f"disk.{self.field} {'!=' if self.negate else '=='} {_quote(self.value)}"

    def test(self, disk: Disk, system: bool) -> bool:
        return (getattr(disk, self.field) == self.value) != self.negate


@dataclass(frozen=True)
class Flag:
    field: str  # rotational | readonly | cdrom
    expected: bool

    def cel(self) -> str:
        return f"{'' if self.expected else '!'}disk.{self.field}"

    def test(self, disk: Disk, system: bool) -> bool:
        return getattr(disk, self.field) == self.expected


@dataclass(frozen=True)
class SystemDisk:
    expected: bool

    def cel(self) -> str:
        return "system_disk" if self.expected else "!system_disk"

    def test(self, disk: Disk, system: bool) -> bool:
        return system == self.expected


@dataclass(frozen=True)
class SizeRange:
    low_gib: int
    high_gib: int

    def cel(self) -> str:
        return f"disk.size >= {self.low_gib}u * GiB && disk.size <= {self.high_gib}u * GiB"

    def test(self, disk: Disk, system: bool) -> bool:
        return self.low_gib * GiB <= disk.size <= self.high_gib * GiB


Predicate = Equals | Flag | SystemDisk | SizeRange


@dataclass(frozen=True)
class Selector:
    terms: tuple[Predicate, ...] = ()
    raw: str | None = None  # a hand-written expression; not evaluable

    @property
    def verifiable(self) -> bool:
        return self.raw is None

    def to_cel(self) -> str:
        if self.raw is not None:
            return self.raw
        return " && ".join(term.cel() for term in self.terms)

    def matches(self, disk: Disk, system: bool) -> bool:
        if self.raw is not None:
            raise ValueError(f"cannot evaluate custom selector {self.raw!r}")
        return all(term.test(disk, system) for term in self.terms)


_TERMS: list[tuple[re.Pattern[str], Callable[[re.Match[str]], Predicate]]] = [
    (
        re.compile(r"disk\.size >= (\d+)u \* GiB && disk\.size <= (\d+)u \* GiB"),
        lambda m: SizeRange(int(m[1]), int(m[2])),
    ),
    (
        re.compile(r'disk\.(transport|model|serial|wwid) (==|!=) "((?:[^"\\]|\\.)*)"'),
        lambda m: Equals(m[1], _unquote(m[3]), m[2] == "!="),
    ),
    (re.compile(r"(!?)disk\.(rotational|readonly|cdrom)\b"), lambda m: Flag(m[2], m[1] == "")),
    (re.compile(r"(!?)system_disk\b"), lambda m: SystemDisk(m[1] == "")),
]


def parse(text: str) -> Selector:
    """Selector for an expression from a document; exact inverse of `to_cel` for generated ones."""
    terms: list[Predicate] = []
    position = 0
    while True:
        for pattern, build in _TERMS:
            match = pattern.match(text, position)
            if match is not None:
                terms.append(build(match))
                position = match.end()
                break
        else:
            return Selector(raw=text)
        if position == len(text):
            return Selector(tuple(terms))
        if not text.startswith(" && ", position):
            return Selector(raw=text)
        position += len(" && ")


def is_system(disk: Disk, system_disk: str | None, *, install: bool) -> bool:
    """`system_disk` as Talos binds it: always false while matching the install disk."""
    return not install and disk.id == system_disk


def _size_range(disk: Disk) -> SizeRange:
    return SizeRange(math.floor(disk.size * 0.9 / GiB), math.ceil(disk.size * 1.1 / GiB))


def _stages(target: Disk) -> list[list[Predicate]]:
    """Candidate identifying terms, most portable first (spec §4)."""
    transport = Equals("transport", target.transport)
    rotational = Flag("rotational", target.rotational)
    stages: list[list[Predicate]] = [
        [transport],
        [transport, rotational],
        [transport, rotational, _size_range(target)],
        [transport, rotational, _size_range(target), Equals("model", target.model)],
    ]
    if target.serial:
        stages.append([Equals("serial", target.serial)])
    if target.wwid:
        stages.append([Equals("wwid", target.wwid)])
    return stages


def _exclusions(target: Disk, identifying: list[Predicate]) -> list[Predicate]:
    terms: list[Predicate] = [Flag("readonly", False), Flag("cdrom", False)]
    pins_transport = any(isinstance(t, Equals) and t.field == "transport" and not t.negate for t in identifying)
    if target.transport != "usb" and not pins_transport:
        terms.append(Equals("transport", "usb", negate=True))
    return terms


def suggest(disks: Sequence[Disk], target: Disk, system_disk: str | None, *, install: bool) -> list[Selector]:
    """Every stage selector that matches exactly `target` among `disks`, most portable first."""
    system_terms: list[Predicate] = []
    if not install and system_disk is not None:
        system_terms = [SystemDisk(target.id == system_disk)]
    found: list[Selector] = []
    for identifying in _stages(target):
        selector = Selector(tuple([*_exclusions(target, identifying), *identifying, *system_terms]))
        hits = [d for d in disks if selector.matches(d, is_system(d, system_disk, install=install))]
        if hits == [target] and selector not in found:
            found.append(selector)
    return found
```

Note: the rotational stage is skipped as a duplicate only when it equals an earlier one; stage 2 differs from stage 1 by the `rotational` term, so both can appear in `found` — the test only checks the first.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/talos/volumes/test_selector.py -q`
Expected: PASS. If `test_size_range_then_model_then_serial` fails on the exact bounds, recompute: 2048 GiB × 0.9 = 1843.2 → floor 1843; × 1.1 = 2252.8 → ceil 2253.

- [ ] **Step 5: Commit**

```bash
git add src/talkub/talos/volumes/selector.py tests/talos/volumes/test_selector.py
git commit -m "feat(volumes): CEL disk selectors with evaluation, parsing and suggestions"
```

---

### Task 3: Validation and version floors

**Files:**
- Create: `src/talkub/talos/volumes/validate.py`
- Test: `tests/talos/volumes/test_volume_validate.py`

**Interfaces:**
- Consumes: `model.*`, `sizes.parse_size`
- Produces: `version_tuple(text) -> tuple[int, int]`, `check_volume(spec, talos_version) -> None` (raises `ValidationError`), `supports(talos_version, feature: str) -> bool` with features `"disk"`, `"percent"`, `"negative"`, `"install"`, `"btrfs"`, `FILESYSTEMS: tuple[str, ...]`, `NAME: re.Pattern`

- [ ] **Step 1: Write the failing tests**

`tests/talos/volumes/test_volume_validate.py`:

```python
import pytest

from talkub.core.errors import ValidationError
from talkub.talos.volumes.model import Kind, VolumeSpec
from talkub.talos.volumes.selector import parse
from talkub.talos.volumes.sizes import parse_size
from talkub.talos.volumes.validate import check_volume, supports, version_tuple

SEL = parse('disk.transport == "nvme"')


def _spec(**kw):
    base = dict(name="data", kind=Kind.PARTITION, selector=SEL, min_size=parse_size("10GiB"), max_size=None)
    base.update(kw)
    return VolumeSpec(**base)


def test_version_tuple():
    assert version_tuple("v1.14.0") == (1, 14)
    assert version_tuple("1.12") == (1, 12)
    with pytest.raises(ValidationError):
        version_tuple("latest")


@pytest.mark.parametrize(
    "feature, version, expected",
    [
        ("disk", "v1.11.5", False),
        ("disk", "v1.12.0", True),
        ("percent", "v1.12.0", True),
        ("negative", "v1.12.9", False),
        ("negative", "v1.13.0", True),
        ("install", "v1.13.4", False),
        ("install", "v1.14.0", True),
        ("btrfs", "v1.14.0", True),
    ],
)
def test_supports(feature, version, expected):
    assert supports(version, feature) is expected


def test_valid_partition_passes():
    check_volume(_spec(), "v1.14.0")


@pytest.mark.parametrize(
    "spec, version, message",
    [
        (_spec(name="x" * 35), "v1.14.0", "name"),
        (_spec(name="da_ta"), "v1.14.0", "name"),
        (_spec(min_size=None, max_size=None), "v1.14.0", "min size or max size"),
        (_spec(min_size=parse_size("20GiB"), max_size=parse_size("10GiB")), "v1.14.0", "larger"),
        (_spec(max_size=parse_size("50%")), "v1.11.0", "v1.12"),
        (_spec(max_size=parse_size("-5GiB")), "v1.12.0", "v1.13"),
        (_spec(kind=Kind.RAW, max_size=parse_size("-5GiB")), "v1.14.0", "raw"),
        (_spec(max_size=parse_size("50%"), grow=True), "v1.14.0", "absolute"),
        (_spec(kind=Kind.DISK, min_size=None), "v1.11.0", "v1.12"),
        (_spec(kind=Kind.DISK), "v1.14.0", "whole-disk"),
        (_spec(filesystem="btrfs"), "v1.13.0", "v1.14"),
        (_spec(filesystem="zfs"), "v1.14.0", "filesystem"),
        (_spec(), "v1.10.0", "v1.11"),
        (_spec(min_size=parse_size("50%")), "v1.14.0", "min size"),
    ],
)
def test_rejects(spec, version, message):
    with pytest.raises(ValidationError, match=message):
        check_volume(spec, version)


def test_disk_volume_without_sizes_passes():
    check_volume(_spec(kind=Kind.DISK, min_size=None), "v1.12.0")


def test_ephemeral_skips_name_rule():
    check_volume(VolumeSpec("EPHEMERAL", Kind.EPHEMERAL, None, max_size=parse_size("40GiB")), "v1.11.0")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/talos/volumes/test_volume_validate.py -q`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

`src/talkub/talos/volumes/validate.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/talos/volumes/test_volume_validate.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/talkub/talos/volumes/validate.py tests/talos/volumes/test_volume_validate.py
git commit -m "feat(volumes): validate volume specs against Talos rules and versions"
```

---

### Task 4: Parse `talosctl get disks|systemdisk|discoveredvolumes`

**Files:**
- Modify: `src/talkub/talos/inspect.py` (append at the end)
- Test: `tests/talos/test_inspect_disks.py`

**Interfaces:**
- Consumes: `inspect.json_documents`, `inspect._dict`, `model.Disk`, `model.Discovered`
- Produces: `inspect.disks(text) -> list[Disk]` (sorted by id), `inspect.system_disk(text) -> str | None`, `inspect.discovered_volumes(text) -> list[Discovered]`

- [ ] **Step 1: Write the failing tests**

`tests/talos/test_inspect_disks.py`:

```python
import json

from talkub.talos import inspect
from talkub.talos.volumes.model import Discovered, Disk


def _stream(*docs):
    # talosctl -o json prints concatenated, indented objects, not an array
    return "".join(json.dumps(doc, indent=4) + "\n" for doc in docs)


def _disk(id, **spec):
    return {"metadata": {"namespace": "runtime", "type": "Disks.block.talos.dev", "id": id}, "node": "10.0.0.5", "spec": spec}


DISKS = _stream(
    _disk(
        "sda",
        dev_path="/dev/sda",
        size=4000787030016,
        pretty_size="4.0 TB",
        io_size=4096,
        sector_size=512,
        readonly=False,
        cdrom=False,
        model="WDC WD40EFRX  ",
        serial="WD-1",
        transport="sata",
        rotational=True,
    ),
    _disk(
        "nvme0n1",
        dev_path="/dev/nvme0n1",
        size=512110190592,
        pretty_size="512 GB",
        readonly=False,
        cdrom=False,
        transport="nvme",
        wwid="eui.1",
    ),
    _disk("sr0", dev_path="/dev/sr0", size=0, readonly=True, cdrom=True),
)


def test_disks_parses_and_defaults_omitted_fields():
    assert inspect.disks(DISKS) == [
        Disk("nvme0n1", "/dev/nvme0n1", 512110190592, transport="nvme", wwid="eui.1"),
        Disk("sda", "/dev/sda", 4000787030016, transport="sata", rotational=True, model="WDC WD40EFRX  ", serial="WD-1"),
    ]


def test_disks_skips_garbage_and_stops_at_invalid_json():
    text = _stream({"spec": "nope"}, _disk("vda", dev_path="/dev/vda", size=10, readonly=False)) + "{broken"
    assert [d.id for d in inspect.disks(text)] == ["vda"]
    assert inspect.disks("") == []


def test_size_true_is_not_an_int():
    assert inspect.disks(_stream(_disk("x", size=True))) == []


def test_system_disk():
    text = _stream({"metadata": {"id": "system-disk"}, "spec": {"diskID": "nvme0n1", "devPath": "/dev/nvme0n1"}})
    assert inspect.system_disk(text) == "nvme0n1"
    assert inspect.system_disk("") is None


def test_discovered_volumes():
    text = _stream(
        {"metadata": {"id": "nvme0n1"}, "spec": {"dev_path": "/dev/nvme0n1", "type": "disk", "size": 512110190592, "name": "gpt"}},
        {
            "metadata": {"id": "nvme0n1p6"},
            "spec": {
                "dev_path": "/dev/nvme0n1p6",
                "type": "partition",
                "size": 100 * 1024**3,
                "offset": 2310012928,
                "name": "xfs",
                "parent_dev_path": "/dev/nvme0n1",
                "partition_label": "EPHEMERAL",
            },
        },
        {"metadata": {"id": "loop0"}, "spec": {"type": "disk"}},
    )
    assert inspect.discovered_volumes(text) == [
        Discovered("/dev/nvme0n1", "disk", 512110190592, name="gpt"),
        Discovered(
            "/dev/nvme0n1p6",
            "partition",
            100 * 1024**3,
            name="xfs",
            parent_dev_path="/dev/nvme0n1",
            offset=2310012928,
            label="EPHEMERAL",
        ),
    ]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/talos/test_inspect_disks.py -q`
Expected: FAIL with `AttributeError: module 'talkub.talos.inspect' has no attribute 'disks'`

- [ ] **Step 3: Implement** — append to `src/talkub/talos/inspect.py` (add `from talkub.talos.volumes.model import Discovered, Disk` to the imports):

```python
def _int(value: object) -> int | None:
    """An integer JSON value; bools are not sizes."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def disks(text: str) -> list[Disk]:
    """`talosctl get disks -o json`, sorted by id. talosctl omits empty/false fields (omitempty);
    they default to "" / False. Entries without an id or a positive size are skipped."""
    result: list[Disk] = []
    for doc in json_documents(text):
        if not isinstance(doc, dict):
            continue
        disk_id = _dict(doc.get("metadata")).get("id")
        spec = _dict(doc.get("spec"))
        size = _int(spec.get("size"))
        if not isinstance(disk_id, str) or size is None or size <= 0:
            continue
        result.append(
            Disk(
                id=disk_id,
                dev_path=_str(spec.get("dev_path")) or f"/dev/{disk_id}",
                size=size,
                transport=_str(spec.get("transport")),
                rotational=spec.get("rotational") is True,
                readonly=spec.get("readonly") is True,
                cdrom=spec.get("cdrom") is True,
                model=_str(spec.get("model")),
                serial=_str(spec.get("serial")),
                wwid=_str(spec.get("wwid")),
            )
        )
    return sorted(result, key=lambda disk: disk.id)


def system_disk(text: str) -> str | None:
    """Disk id from `talosctl get systemdisk -o json`; None before installation."""
    for doc in json_documents(text):
        disk_id = _dict(_dict(doc).get("spec")).get("diskID")
        if isinstance(disk_id, str) and disk_id:
            return disk_id
    return None


def discovered_volumes(text: str) -> list[Discovered]:
    """`talosctl get discoveredvolumes -o json`: what is on each disk and partition."""
    result: list[Discovered] = []
    for doc in json_documents(text):
        spec = _dict(_dict(doc).get("spec"))
        size = _int(spec.get("size"))
        dev_path = _str(spec.get("dev_path"))
        if not dev_path or size is None:
            continue
        result.append(
            Discovered(
                dev_path=dev_path,
                type=_str(spec.get("type")),
                size=size,
                name=_str(spec.get("name")),
                parent_dev_path=_str(spec.get("parent_dev_path")),
                offset=_int(spec.get("offset")),
                label=_str(spec.get("partition_label")),
            )
        )
    return result
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/talos/test_inspect_disks.py tests/talos/test_inspect.py -q && uv run lint-imports`
Expected: PASS; import contract kept

- [ ] **Step 5: Commit**

```bash
git add src/talkub/talos/inspect.py tests/talos/test_inspect_disks.py
git commit -m "feat(inspect): parse talosctl disks, systemdisk and discoveredvolumes"
```

---

### Task 5: Allocation simulation

**Files:**
- Create: `src/talkub/talos/volumes/simulate.py`
- Test: `tests/talos/volumes/test_simulate.py`

**Interfaces:**
- Consumes: `model.*`, `selector.is_system`, `sizes.format_bytes`
- Produces:
  - constants `SYSTEM_LAYOUT = 2202 * MiB`, `DEFAULT_MIN = 100 * MiB`, `EPHEMERAL_MIN = 2 * GiB`
  - `DEFAULT_EPHEMERAL: VolumeSpec` (Talos default: system disk, min 2 GiB, no max, grow)
  - `Allocation(volume: str, disk: str, size: int, existing: bool = False)` (frozen)
  - `Plan(allocations: list[Allocation], errors: dict[str, str], warnings: list[str], free: dict[str, int], reserved: dict[str, int], inexact: bool)`
  - `simulate(inventory, volumes, *, system_disk: str | None, install_disk: str | None) -> Plan`

Semantics (spec §2, §7): order by (wave: EPHEMERAL −1 else 0, grow, absolute max bytes or ∞, volume id); candidates are non-readonly disks the selector matches, in id order; `partition`/`raw`/EPHEMERAL take the first candidate whose largest free region ≥ min (default 100 MiB, EPHEMERAL 2 GiB); size = region if no max, `min(max, region)` for bytes, `region*p//100` for percent, `region−x` for negative; aligned down to 1 MiB; `disk` needs exactly one candidate and a blank disk. Volumes already on the node (partition label equals the volume id) are reported as existing and consume nothing. The install disk (maintenance mode, `wipe: true`) is empty except `SYSTEM_LAYOUT`.

- [ ] **Step 1: Write the failing tests**

`tests/talos/volumes/test_simulate.py`:

```python
from talkub.talos.volumes.model import GiB, MiB, Discovered, Disk, Inventory, Kind, VolumeSpec
from talkub.talos.volumes.selector import parse
from talkub.talos.volumes.simulate import DEFAULT_EPHEMERAL, SYSTEM_LAYOUT, simulate
from talkub.talos.volumes.sizes import parse_size

NVME = Disk("nvme0n1", "/dev/nvme0n1", 512 * GiB, transport="nvme")
SSD = Disk("sda", "/dev/sda", 1024 * GiB, transport="sata")
SSD2 = Disk("sdb", "/dev/sdb", 1024 * GiB, transport="sata")
USABLE_NVME = 512 * GiB - 2 * MiB  # 1 MiB GPT head and tail


def _inv(*disks, discovered=(), system=None, installed=False):
    return Inventory(tuple(disks), tuple(discovered), system, installed)


def _vol(name, match, kind=Kind.PARTITION, min_size=None, max_size=None, grow=False):
    return VolumeSpec(
        name,
        kind,
        parse(match),
        parse_size(min_size) if min_size else None,
        parse_size(max_size) if max_size else None,
        grow,
    )


def _sizes(plan):
    return {a.volume: (a.disk, a.size) for a in plan.allocations}


def test_default_ephemeral_takes_the_whole_system_disk():
    plan = simulate(
        _inv(NVME),
        [DEFAULT_EPHEMERAL, _vol("local", "system_disk", min_size="100GiB", max_size="100GiB")],
        system_disk="nvme0n1",
        install_disk="nvme0n1",
    )
    assert _sizes(plan)["EPHEMERAL"] == ("nvme0n1", USABLE_NVME - SYSTEM_LAYOUT)
    assert "not enough space" in plan.errors["u-local"]
    assert "limit EPHEMERAL" in plan.errors["u-local"]
    assert plan.reserved == {"nvme0n1": SYSTEM_LAYOUT}


def test_limited_ephemeral_leaves_room():
    ephemeral = VolumeSpec("EPHEMERAL", Kind.EPHEMERAL, None, parse_size("40GiB"), parse_size("40GiB"))
    plan = simulate(
        _inv(NVME),
        [ephemeral, _vol("local", "system_disk", min_size="100GiB", max_size="100GiB")],
        system_disk="nvme0n1",
        install_disk="nvme0n1",
    )
    assert _sizes(plan) == {"EPHEMERAL": ("nvme0n1", 40 * GiB), "u-local": ("nvme0n1", 100 * GiB)}
    assert plan.free["nvme0n1"] == USABLE_NVME - SYSTEM_LAYOUT - 140 * GiB
    assert plan.errors == {}


def test_percent_is_of_the_free_region_and_negative_subtracts():
    plan = simulate(
        _inv(SSD, SSD2),
        [
            _vol("half", 'disk.transport == "sata" && disk.serial == ""', min_size="100MiB", max_size="50%"),
        ],
        system_disk=None,
        install_disk=None,
    )
    free = 1024 * GiB - 2 * MiB
    assert _sizes(plan)["u-half"] == ("sda", (free * 50 // 100) // MiB * MiB)
    assert any("matches sda, sdb" in w for w in plan.warnings)
    negative = simulate(_inv(SSD), [_vol("most", "!system_disk", min_size="100MiB", max_size="-24GiB")], system_disk=None, install_disk=None)
    assert _sizes(negative)["u-most"] == ("sda", free - 24 * GiB)


def test_order_non_grow_and_smaller_max_first_then_falls_through():
    volumes = [
        _vol("big", 'disk.transport == "sata"', min_size="1GiB", grow=True),
        _vol("small", 'disk.transport == "sata"', min_size="600GiB", max_size="600GiB"),
        _vol("mid", 'disk.transport == "sata"', min_size="700GiB", max_size="700GiB"),
    ]
    plan = simulate(_inv(SSD, SSD2), volumes, system_disk=None, install_disk=None)
    assert [a.volume for a in plan.allocations] == ["u-small", "u-mid", "u-big"]
    # small fills sda to 600, mid does not fit next to it and falls through to sdb
    assert _sizes(plan)["u-mid"][0] == "sdb"
    assert _sizes(plan)["u-big"] == ("sda", 1024 * GiB - 2 * MiB - 600 * GiB)


def test_no_match_and_readonly():
    ro = Disk("sr0", "/dev/sr0", GiB, transport="sata", readonly=True)
    plan = simulate(_inv(ro), [_vol("x", 'disk.transport == "sata"', min_size="100MiB")], system_disk=None, install_disk=None)
    assert plan.errors["u-x"] == "selector matches no disk"


def test_whole_disk_needs_one_blank_match():
    several = simulate(_inv(SSD, SSD2), [_vol("d", 'disk.transport == "sata"', kind=Kind.DISK)], system_disk=None, install_disk=None)
    assert "several disks" in several.errors["u-d"]
    used = Discovered("/dev/sda", "disk", SSD.size, name="ntfs")
    plan = simulate(_inv(SSD, discovered=[used]), [_vol("d", 'disk.transport == "sata"', kind=Kind.DISK)], system_disk=None, install_disk=None)
    assert "not empty" in plan.errors["u-d"]
    blank = simulate(_inv(SSD), [_vol("d", 'disk.transport == "sata"', kind=Kind.DISK)], system_disk=None, install_disk=None)
    assert _sizes(blank)["u-d"] == ("sda", SSD.size)


def test_existing_partitions_limit_free_space_and_foreign_format_is_rejected():
    discovered = [
        Discovered("/dev/sda", "disk", SSD.size, name="gpt"),
        Discovered("/dev/sda1", "partition", 900 * GiB, name="ext4", parent_dev_path="/dev/sda", offset=MiB),
        Discovered("/dev/sdb", "disk", SSD2.size, name="ntfs"),
    ]
    plan = simulate(
        _inv(SSD, SSD2, discovered=discovered),
        [_vol("x", 'disk.transport == "sata"', min_size="200GiB")],
        system_disk=None,
        install_disk=None,
    )
    assert "sda: not enough space" in plan.errors["u-x"]
    assert "sdb: not GPT (ntfs)" in plan.errors["u-x"]


def test_missing_offsets_mark_the_plan_inexact():
    discovered = [
        Discovered("/dev/sda", "disk", SSD.size, name="gpt"),
        Discovered("/dev/sda1", "partition", 100 * GiB, parent_dev_path="/dev/sda"),
    ]
    plan = simulate(_inv(SSD, discovered=discovered), [_vol("x", "!system_disk", min_size="1GiB")], system_disk=None, install_disk=None)
    assert plan.inexact
    assert _sizes(plan)["u-x"] == ("sda", 1024 * GiB - 2 * MiB - 100 * GiB)


def test_existing_volume_is_located_by_label():
    discovered = [
        Discovered("/dev/sda", "disk", SSD.size, name="gpt"),
        Discovered("/dev/sda1", "partition", 300 * GiB, name="xfs", parent_dev_path="/dev/sda", offset=MiB, label="u-data"),
    ]
    plan = simulate(
        _inv(SSD, discovered=discovered, system="nvme0n1", installed=True),
        [_vol("data", 'disk.model == "gone"', min_size="1GiB")],
        system_disk="nvme0n1",
        install_disk=None,
    )
    assert plan.allocations[0].existing and _sizes(plan)["u-data"] == ("sda", 300 * GiB)


def test_custom_selector_is_skipped_with_warning():
    plan = simulate(_inv(SSD), [_vol("x", "disk.size > 1u * GB", min_size="1GiB")], system_disk=None, install_disk=None)
    assert plan.allocations == [] and "not simulated" in plan.warnings[0]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/talos/volumes/test_simulate.py -q`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

`src/talkub/talos/volumes/simulate.py`:

```python
"""Predict where Talos puts each volume and how large it becomes (spec §2, §7).

Mirrors Talos' volume manager: `CompareVolumeConfigs` ordering, first candidate disk whose
largest contiguous free region holds `minSize`, and `CreatePartition` sizing. Sizes on the install
disk and after partitions without offsets are approximate (`Plan.inexact`).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from talkub.talos.volumes.model import GiB, MiB, Disk, Inventory, Kind, Size, SizeKind, VolumeSpec
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
            plan.warnings.append(
                f"{vid}: selector matches {', '.join(d.id for d in candidates)}; Talos takes the first with enough space"
            )
        need = volume.min_size.value if volume.min_size else (EPHEMERAL_MIN if volume.kind is Kind.EPHEMERAL else DEFAULT_MIN)
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/talos/volumes/test_simulate.py -q`
Expected: PASS. If `test_order_non_grow_and_smaller_max_first_then_falls_through` fails, check `_order`: `grow=False` sorts before `True`, and `u-small` (600 GiB) before `u-mid` (700 GiB).

- [ ] **Step 5: Commit**

```bash
git add src/talkub/talos/volumes/simulate.py tests/talos/volumes/test_simulate.py
git commit -m "feat(volumes): simulate Talos volume allocation"
```

---

### Task 6: Documents and patch merge

**Files:**
- Create: `src/talkub/talos/volumes/render.py`
- Modify: `src/talkub/talos/nodes.py` (add `read_node_documents`, `upsert_documents`)
- Modify: `src/talkub/cli/nodes.py:79` (next hint)
- Test: `tests/talos/volumes/test_volume_render.py`
- Test: `tests/talos/test_nodes.py` (append), `tests/cli/test_cli_nodes.py` (extend first test)

**Interfaces:**
- Consumes: `model.*`, `selector.Selector`, `selector.parse`, `sizes.parse_size`
- Produces:
  - `render.ENCRYPTION: dict`, `render.volume_document(spec) -> dict`, `render.install_document(selector, existing: Mapping | None) -> dict`, `render.spec_from_document(doc) -> VolumeSpec | None`
  - `nodes.read_node_documents(path) -> list[dict]`, `nodes.upsert_documents(path, docs) -> None`

- [ ] **Step 1: Write the failing tests**

`tests/talos/volumes/test_volume_render.py`:

```python
import pytest
from helpers import need

from talkub.talos.volumes.model import Kind, VolumeSpec
from talkub.talos.volumes.render import ENCRYPTION, install_document, spec_from_document, volume_document
from talkub.talos.volumes.selector import parse
from talkub.talos.volumes.sizes import parse_size

NVME = parse('!disk.readonly && !disk.cdrom && disk.transport == "nvme" && !system_disk')


def test_partition_document():
    spec = VolumeSpec("data", Kind.PARTITION, NVME, parse_size("100GiB"), parse_size("100GiB"), encrypted=True)
    assert volume_document(spec) == {
        "apiVersion": "v1alpha1",
        "kind": "UserVolumeConfig",
        "name": "data",
        "provisioning": {"diskSelector": {"match": NVME.to_cel()}, "minSize": "100GiB", "maxSize": "100GiB"},
        "filesystem": {"type": "xfs"},
        "encryption": ENCRYPTION,
    }
    assert ENCRYPTION == {"provider": "luks2", "keys": [{"slot": 0, "nodeID": {}, "lockToState": True}]}


def test_raw_disk_and_ephemeral_documents():
    raw = volume_document(VolumeSpec("ceph", Kind.RAW, NVME, parse_size("1GiB"), grow=True))
    assert raw["kind"] == "RawVolumeConfig" and "filesystem" not in raw and raw["provisioning"]["grow"] is True
    disk = volume_document(VolumeSpec("lvm", Kind.DISK, NVME, filesystem="ext4"))
    assert disk["volumeType"] == "disk" and disk["provisioning"] == {"diskSelector": {"match": NVME.to_cel()}}
    ephemeral = volume_document(VolumeSpec("EPHEMERAL", Kind.EPHEMERAL, None, parse_size("100MiB"), parse_size("40%")))
    assert ephemeral == {
        "apiVersion": "v1alpha1",
        "kind": "VolumeConfig",
        "name": "EPHEMERAL",
        "provisioning": {"minSize": "100MiB", "maxSize": "40%", "grow": False},
    }


def test_install_document_keeps_installer_and_wipe():
    existing = {
        "apiVersion": "v1alpha1",
        "kind": "UnattendedInstallConfig",
        "installer": {"image": "x"},
        "provisioning": {"wipe": False, "diskSelector": {"match": "old"}},
    }
    doc = install_document(NVME, existing)
    assert doc["installer"] == {"image": "x"}
    assert doc["provisioning"] == {"wipe": False, "diskSelector": {"match": NVME.to_cel()}}
    assert existing["provisioning"]["diskSelector"]["match"] == "old"
    assert install_document(NVME, None)["kind"] == "UnattendedInstallConfig"


@pytest.mark.parametrize(
    "spec",
    [
        VolumeSpec("data", Kind.PARTITION, NVME, parse_size("10GiB"), parse_size("10GiB"), filesystem="ext4"),
        VolumeSpec("ceph", Kind.RAW, NVME, parse_size("1GiB"), encrypted=True),
        VolumeSpec("lvm", Kind.DISK, NVME),
        VolumeSpec("EPHEMERAL", Kind.EPHEMERAL, None, parse_size("40GiB"), parse_size("40GiB")),
    ],
)
def test_spec_round_trips(spec):
    assert spec_from_document(volume_document(spec)) == spec


def test_spec_from_foreign_documents():
    assert spec_from_document({"kind": "HostnameConfig"}) is None
    assert spec_from_document({"kind": "UserVolumeConfig", "name": "d", "volumeType": "directory"}) is None
    custom = spec_from_document(
        {"kind": "UserVolumeConfig", "name": "d", "provisioning": {"diskSelector": {"match": "disk.size > 1u * GB"}, "minSize": "2GB"}}
    )
    assert custom is not None and not custom.selector.verifiable


@need("talosctl")
def test_documents_pass_talosctl_validate(tmp_path):
    import subprocess

    from talkub.core import yamlio

    docs = [
        volume_document(VolumeSpec("EPHEMERAL", Kind.EPHEMERAL, None, parse_size("40GiB"), parse_size("40GiB"))),
        volume_document(VolumeSpec("data", Kind.PARTITION, NVME, parse_size("10GiB"), parse_size("50%"))),
        volume_document(VolumeSpec("ceph", Kind.RAW, parse('disk.size >= 90u * GiB && disk.size <= 110u * GiB'), parse_size("1GiB"), encrypted=True)),
        volume_document(VolumeSpec("lvm", Kind.DISK, parse('disk.serial == "a\\"b"'), filesystem="btrfs")),
    ]
    patch = tmp_path / "patch.yaml"
    patch.write_text("---\n".join(yamlio.dump(d) for d in docs))
    config = subprocess.run(
        ["talosctl", "gen", "config", "t", "https://10.0.0.1:6443", "-t", "controlplane", "-o", "-",
         "--talos-version", "v1.14.0", "--with-docs=false", "--with-examples=false", "--config-patch", f"@{patch}"],
        capture_output=True, check=True,
    ).stdout
    rendered = tmp_path / "cp.yaml"
    rendered.write_bytes(config)
    result = subprocess.run(["talosctl", "validate", "--config", str(rendered), "--mode", "metal"], capture_output=True)
    assert result.returncode == 0, result.stderr.decode() + result.stdout.decode()
```

Append to `tests/talos/test_nodes.py`:

```python
def test_upsert_documents_replaces_by_kind_and_name_and_keeps_the_rest(tmp_path):
    from talkub.talos.nodes import read_node_documents, upsert_documents

    path = tmp_path / "cp-01.yaml"
    path.write_text(render_node_patch("cp-01", NodeOptions(disk_selector="old", dns=("1.1.1.1",))))
    upsert_documents(
        path,
        [
            {"apiVersion": "v1alpha1", "kind": "UnattendedInstallConfig", "provisioning": {"diskSelector": {"match": "new"}}},
            {"apiVersion": "v1alpha1", "kind": "UserVolumeConfig", "name": "data"},
        ],
    )
    text = path.read_text()
    assert text.startswith("# Talos patch for node cp-01")
    assert text.count("# Talos patch for node cp-01") == 1
    docs = read_node_documents(path)
    assert [d["kind"] for d in docs] == ["HostnameConfig", "ResolverConfig", "UnattendedInstallConfig", "UserVolumeConfig"]
    assert docs[2]["provisioning"]["diskSelector"]["match"] == "new"
    upsert_documents(path, [{"apiVersion": "v1alpha1", "kind": "UserVolumeConfig", "name": "data", "x": 1}])
    assert [d.get("x") for d in read_node_documents(path) if d["kind"] == "UserVolumeConfig"] == [1]
```

In `tests/cli/test_cli_nodes.py::test_node_add_static` add after the existing `"apply --insecure w-01"` assertion:

```python
    assert "talkub node volumes w-01" in captured.out
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/talos/volumes/test_volume_render.py tests/talos/test_nodes.py tests/cli/test_cli_nodes.py -q`
Expected: FAIL (`ModuleNotFoundError` for render; `ImportError` for `upsert_documents`; missing hint)

- [ ] **Step 3: Implement**

`src/talkub/talos/volumes/render.py`:

```python
"""Talos documents for volume specs, and specs back from documents already in a node patch."""

from __future__ import annotations

import copy
from collections.abc import Mapping

from talkub.core.errors import ValidationError
from talkub.talos.volumes.model import Kind, Size, VolumeSpec
from talkub.talos.volumes.selector import Selector, parse
from talkub.talos.volumes.sizes import parse_size

ENCRYPTION = {"provider": "luks2", "keys": [{"slot": 0, "nodeID": {}, "lockToState": True}]}
_KIND = {Kind.PARTITION: "UserVolumeConfig", Kind.DISK: "UserVolumeConfig", Kind.RAW: "RawVolumeConfig"}


def volume_document(spec: VolumeSpec) -> dict:
    provisioning: dict = {}
    if spec.selector is not None:
        provisioning["diskSelector"] = {"match": spec.selector.to_cel()}
    if spec.min_size is not None:
        provisioning["minSize"] = spec.min_size.text
    if spec.max_size is not None:
        provisioning["maxSize"] = spec.max_size.text
    if spec.kind is Kind.EPHEMERAL:
        provisioning["grow"] = spec.grow  # explicit: Talos defaults EPHEMERAL to grow
    elif spec.grow:
        provisioning["grow"] = True
    if spec.kind is Kind.EPHEMERAL:
        doc: dict = {"apiVersion": "v1alpha1", "kind": "VolumeConfig", "name": "EPHEMERAL"}
    else:
        doc = {"apiVersion": "v1alpha1", "kind": _KIND[spec.kind], "name": spec.name}
        if spec.kind is Kind.DISK:
            doc["volumeType"] = "disk"
    doc["provisioning"] = provisioning
    if spec.kind in (Kind.PARTITION, Kind.DISK):
        doc["filesystem"] = {"type": spec.filesystem}
    if spec.encrypted:
        doc["encryption"] = copy.deepcopy(ENCRYPTION)
    return doc


def install_document(selector: Selector, existing: Mapping | None) -> dict:
    """The node's UnattendedInstallConfig with a new disk selector; other fields are kept."""
    doc = copy.deepcopy(dict(existing)) if existing else {"apiVersion": "v1alpha1", "kind": "UnattendedInstallConfig"}
    provisioning = dict(doc.get("provisioning") or {})
    provisioning["diskSelector"] = {"match": selector.to_cel()}
    doc["provisioning"] = provisioning
    return doc


def _size(value: object) -> Size | None:
    if value is None:
        return None
    try:
        return parse_size(str(value))
    except ValidationError:
        return None


def spec_from_document(doc: object) -> VolumeSpec | None:
    """VolumeSpec for a volume document already in the patch (to simulate it); None otherwise."""
    if not isinstance(doc, Mapping):
        return None
    kind_name, name = doc.get("kind"), str(doc.get("name", ""))
    if kind_name == "VolumeConfig" and name == "EPHEMERAL":
        kind = Kind.EPHEMERAL
    elif kind_name == "RawVolumeConfig":
        kind = Kind.RAW
    elif kind_name == "UserVolumeConfig" and doc.get("volumeType", "partition") in ("partition", "disk"):
        kind = Kind.DISK if doc.get("volumeType") == "disk" else Kind.PARTITION
    else:
        return None
    provisioning = doc.get("provisioning") or {}
    match = (provisioning.get("diskSelector") or {}).get("match")
    return VolumeSpec(
        name=name,
        kind=kind,
        selector=parse(str(match)) if match is not None else None,
        min_size=_size(provisioning.get("minSize")),
        max_size=_size(provisioning.get("maxSize")),
        grow=provisioning.get("grow") is True,
        filesystem=str((doc.get("filesystem") or {}).get("type", "xfs")),
        encrypted="encryption" in doc,
    )
```

Note: `spec_from_document(volume_document(DEFAULT-like EPHEMERAL))` round-trips because `grow` is read back from the explicit `grow: false`.

Add to `src/talkub/talos/nodes.py` (new imports: `import itertools`, `from collections.abc import Sequence`):

```python
def _split_header(text: str) -> tuple[str, str]:
    """(leading comment lines, rest) of a node patch."""
    lines = text.splitlines(keepends=True)
    header = "".join(itertools.takewhile(lambda line: line.startswith("#"), lines))
    return header, text[len(header) :]


def read_node_documents(path: Path) -> list[dict]:
    _, body = _split_header(path.read_text())
    return [doc for doc in yamlio.load_all(body) if doc]


def _doc_key(doc: dict) -> tuple[object, object]:
    # A node has one UnattendedInstallConfig; every other kind talkub writes is keyed by name.
    return doc.get("kind"), None if doc.get("kind") == "UnattendedInstallConfig" else doc.get("name")


def upsert_documents(path: Path, docs: Sequence[dict]) -> None:
    """Replace documents with the same (kind, name) in the node patch, append the others; the
    header comment and all other documents are kept."""
    header, body = _split_header(path.read_text())
    existing = [doc for doc in yamlio.load_all(body) if doc]
    index = {_doc_key(doc): i for i, doc in enumerate(existing)}
    for doc in docs:
        key = _doc_key(doc)
        if key in index:
            existing[index[key]] = doc
        else:
            index[key] = len(existing)
            existing.append(doc)
    atomic_write(path, (header + "---\n".join(yamlio.dump(doc) for doc in existing)).encode())
```

In `src/talkub/cli/nodes.py`, replace the last line of `add`:

```python
    print(
        f"next: boot the node, then optionally `talkub node volumes {name}` (install disk, volumes) "
        f"and `talkub apply --insecure {name}`"
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/talos/volumes/test_volume_render.py tests/talos/test_nodes.py tests/cli/test_cli_nodes.py -q`
Expected: PASS (the talosctl test runs because talosctl v1.14 is installed; if it fails, the stderr in the assertion shows which document Talos rejects — fix the renderer, not the test)

- [ ] **Step 5: Commit**

```bash
git add src/talkub/talos/volumes/render.py src/talkub/talos/nodes.py src/talkub/cli/nodes.py \
  tests/talos/volumes/test_volume_render.py tests/talos/test_nodes.py tests/cli/test_cli_nodes.py
git commit -m "feat(volumes): render volume documents and merge them into node patches"
```

---

### Task 7: Read disks from the node

**Files:**
- Create: `src/talkub/talos/disks.py`
- Test: `tests/talos/test_disks.py`

**Interfaces:**
- Consumes: `inspect.disks`, `inspect.system_disk`, `inspect.discovered_volumes`, `client.TalosSession`, `client.talos_session`, `model.Inventory`
- Produces: `read_insecure(runner, address) -> Inventory`, `read_installed(talos, address) -> Inventory`, `read(ctx, name, mode: str) -> Inventory` with `mode in ("auto", "insecure", "installed")`, `PROBE_TIMEOUT`

- [ ] **Step 1: Write the failing tests**

`tests/talos/test_disks.py`:

```python
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from helpers import FakeRunner

from talkub.core.errors import CommandFailed, ValidationError
from talkub.talos import disks
from talkub.talos.client import TalosSession

DISK = json.dumps({"metadata": {"id": "sda"}, "spec": {"dev_path": "/dev/sda", "size": 1000}})
SYSTEM = json.dumps({"metadata": {"id": "system-disk"}, "spec": {"diskID": "sda"}})
VOLUMES = json.dumps({"metadata": {"id": "sda"}, "spec": {"dev_path": "/dev/sda", "type": "disk", "size": 1000, "name": "gpt"}})


@dataclass
class ScriptedRunner(FakeRunner):
    """Answers by resource name (`disks`, `systemdisk`, …); `fail` makes those exit 1."""

    answers: dict[str, str] = field(default_factory=dict)
    fail: set[str] = field(default_factory=set)

    def run(self, argv, *, input=None, env=None, cwd=None, check=True, passthrough_stderr=False, timeout=None):
        super().run(argv, check=check, timeout=timeout)
        key = next((word for word in argv if word in self.answers or word in self.fail), None)
        code = 1 if key in self.fail else 0
        if code and check:
            raise CommandFailed(f"{argv[0]} failed")
        return subprocess.CompletedProcess(list(argv), code, self.answers.get(key, "").encode(), b"")


@pytest.fixture(autouse=True)
def _talosctl(monkeypatch):
    monkeypatch.setattr(disks, "require_tool", lambda name: None)


def test_read_insecure_uses_maintenance_api_and_tolerates_missing_volumes():
    runner = ScriptedRunner(answers={"disks": DISK}, fail={"discoveredvolumes"})
    inventory = disks.read_insecure(runner, "10.0.0.5")
    assert [d.id for d in inventory.disks] == ["sda"]
    assert inventory.system_disk is None and not inventory.installed and inventory.discovered == ()
    assert runner.calls[0].argv == ["talosctl", "get", "disks", "--insecure", "-n", "10.0.0.5", "-o", "json"]


def test_read_installed_reads_system_disk_and_volumes():
    runner = ScriptedRunner(answers={"disks": DISK, "systemdisk": SYSTEM, "discoveredvolumes": VOLUMES})
    inventory = disks.read_installed(TalosSession(runner, Path("tc")), "cp-01.ts.net")
    assert inventory.installed and inventory.system_disk == "sda" and inventory.discovered[0].name == "gpt"
    assert all("-n" in c.argv and "cp-01.ts.net" in c.argv for c in runner.calls)


def test_no_disks_is_an_error():
    with pytest.raises(ValidationError, match="no disks"):
        disks.read_insecure(ScriptedRunner(answers={"disks": ""}), "10.0.0.5")


def test_auto_falls_back_to_insecure_without_talosconfig(empty_repo, monkeypatch):
    from talkub.context import Context

    runner = ScriptedRunner(answers={"disks": DISK})
    ctx = Context.open(empty_repo, runner)
    inventory = disks.read(ctx, "cp-01", "auto")
    assert not inventory.installed
    assert "--insecure" in runner.calls[0].argv and "10.0.0.10" in runner.calls[0].argv


def test_auto_uses_session_when_node_answers(empty_repo, monkeypatch):
    from contextlib import contextmanager

    from talkub.context import Context

    runner = ScriptedRunner(answers={"version": "ok", "disks": DISK, "systemdisk": SYSTEM})
    empty_repo.talosconfig.parent.mkdir(parents=True, exist_ok=True)
    empty_repo.talosconfig.write_text("x")

    @contextmanager
    def fake_session(ctx):
        yield TalosSession(runner, Path("tc"))

    monkeypatch.setattr(disks, "talos_session", fake_session)
    inventory = disks.read(Context.open(empty_repo, runner), "cp-01", "auto")
    assert inventory.installed and inventory.system_disk == "sda"


def test_auto_falls_back_when_session_probe_fails(empty_repo, monkeypatch):
    from contextlib import contextmanager

    from talkub.context import Context

    runner = ScriptedRunner(answers={"disks": DISK}, fail={"version"})
    empty_repo.talosconfig.parent.mkdir(parents=True, exist_ok=True)
    empty_repo.talosconfig.write_text("x")

    @contextmanager
    def fake_session(ctx):
        yield TalosSession(runner, Path("tc"))

    monkeypatch.setattr(disks, "talos_session", fake_session)
    assert not disks.read(Context.open(empty_repo, runner), "cp-01", "auto").installed
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/talos/test_disks.py -q`
Expected: FAIL with `ImportError: cannot import name 'disks'`

- [ ] **Step 3: Implement**

`src/talkub/talos/disks.py`:

```python
"""Read a node's disks: via the maintenance API (`--insecure`) before installation, via the
talosconfig afterwards."""

from __future__ import annotations

from talkub.context import Context
from talkub.core.errors import ValidationError
from talkub.core.runner import Runner, require_tool
from talkub.talos import inspect
from talkub.talos.client import TalosSession, talos_session
from talkub.talos.volumes.model import Inventory

PROBE_TIMEOUT = 15.0
MODES = ("auto", "insecure", "installed")


def _require_disks(inventory: Inventory, address: str) -> Inventory:
    if not inventory.disks:
        raise ValidationError(f"no disks found on {address}", hint="is the node booted and reachable?")
    return inventory


def read_insecure(runner: Runner, address: str) -> Inventory:
    require_tool("talosctl")

    def get(resource: str, *, check: bool) -> str:
        argv = ["talosctl", "get", resource, "--insecure", "-n", address, "-o", "json"]
        return runner.run(argv, check=check, timeout=PROBE_TIMEOUT).stdout.decode(errors="replace")

    disks = inspect.disks(get("disks", check=True))
    discovered = inspect.discovered_volumes(get("discoveredvolumes", check=False))
    return _require_disks(Inventory(tuple(disks), tuple(discovered), None, installed=False), address)


def read_installed(talos: TalosSession, address: str) -> Inventory:
    def get(resource: str, *, check: bool) -> str:
        result = talos.run(address, ["get", resource, "-o", "json"], check=check, timeout=PROBE_TIMEOUT)
        return result.stdout.decode(errors="replace")

    disks = inspect.disks(get("disks", check=True))
    system = inspect.system_disk(get("systemdisk", check=False))
    discovered = inspect.discovered_volumes(get("discoveredvolumes", check=False))
    return _require_disks(Inventory(tuple(disks), tuple(discovered), system, installed=True), address)


def read(ctx: Context, name: str, mode: str) -> Inventory:
    """Disks of node `name`. `auto` uses the talosconfig if the node answers with it (installed),
    else the maintenance API at the node's `ip`."""
    if mode not in MODES:
        raise ValueError(mode)
    settings = ctx.repo.load_settings()
    node = settings.node(name)
    address = settings.mgmt_address(name)
    if mode == "insecure":
        return read_insecure(ctx.runner, node.ip)
    if mode == "installed":
        with talos_session(ctx) as talos:
            return read_installed(talos, address)
    if ctx.repo.talosconfig.is_file():
        with talos_session(ctx) as talos:
            if talos.run(address, ["version"], check=False, timeout=PROBE_TIMEOUT).returncode == 0:
                return read_installed(talos, address)
    return read_insecure(ctx.runner, node.ip)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/talos/test_disks.py -q && uv run lint-imports`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/talkub/talos/disks.py tests/talos/test_disks.py
git commit -m "feat(disks): read node disks via maintenance API or talosconfig"
```

---

### Task 8: Text report (disk table and plan)

**Files:**
- Create: `src/talkub/talos/volumes/report.py`
- Test: `tests/talos/volumes/test_volume_report.py`

**Interfaces:**
- Consumes: `model.Inventory`, `simulate.Plan`, `sizes.format_bytes`
- Produces: `disk_table(inventory, system_disk: str | None = None) -> str`, `plan_text(plan, inventory, system_disk: str | None) -> str`

- [ ] **Step 1: Write the failing tests**

`tests/talos/volumes/test_volume_report.py`:

```python
from talkub.talos.volumes.model import GiB, Discovered, Disk, Inventory
from talkub.talos.volumes.report import disk_table, plan_text
from talkub.talos.volumes.simulate import SYSTEM_LAYOUT, Allocation, Plan

NVME = Disk("nvme0n1", "/dev/nvme0n1", 512 * GiB, transport="nvme", model="Samsung 990  ", serial="S1")
HDD = Disk("sda", "/dev/sda", 4096 * GiB, transport="sata", rotational=True)
CD = Disk("sr0", "/dev/sr0", GiB, readonly=True, cdrom=True)


def test_disk_table():
    inventory = Inventory(
        (NVME, HDD, CD),
        (Discovered("/dev/sda", "disk", HDD.size, name="gpt"), Discovered("/dev/sda1", "partition", GiB, name="ext4", parent_dev_path="/dev/sda", label="data")),
        None,
        False,
    )
    lines = disk_table(inventory, system_disk="nvme0n1").splitlines()
    assert lines[0].split() == ["#", "DEVICE", "SIZE", "TYPE", "TRANSPORT", "MODEL", "SERIAL", "CONTENT"]
    assert lines[1].split()[:5] == ["1", "/dev/nvme0n1", "512.0", "GiB", "SSD"]
    assert "Samsung 990" in lines[1] and "system" in lines[1]
    assert "HDD" in lines[2] and "gpt: data (ext4)" in lines[2]
    assert "readonly, cdrom" in lines[3]


def test_plan_text():
    plan = Plan(
        allocations=[Allocation("EPHEMERAL", "nvme0n1", 40 * GiB), Allocation("u-data", "sda", 100 * GiB, existing=True)],
        errors={"u-x": "selector matches no disk"},
        warnings=["u-y: selector matches sda, sdb; Talos takes the first with enough space"],
        free={"nvme0n1": 400 * GiB, "sda": 3996 * GiB, "sr0": 0},
        reserved={"nvme0n1": SYSTEM_LAYOUT},
        inexact=True,
    )
    text = plan_text(plan, Inventory((NVME, HDD, CD), (), "nvme0n1", False), "nvme0n1")
    assert "/dev/nvme0n1 ★" in text
    assert "system ≈2.2 GiB │ EPHEMERAL 40.0 GiB │ free 400.0 GiB" in text
    assert "u-data 100.0 GiB (exists)" in text
    assert "sr0" not in text
    assert "✗ u-x: selector matches no disk" in text
    assert "⚠ u-y:" in text
    assert "approximate" in text and "★ = system disk" in text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/talos/volumes/test_volume_report.py -q`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

`src/talkub/talos/volumes/report.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/talos/volumes/test_volume_report.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/talkub/talos/volumes/report.py tests/talos/volumes/test_volume_report.py
git commit -m "feat(volumes): disk table and allocation report"
```

---

### Task 9: `talkub node volumes` dialog

**Files:**
- Create: `src/talkub/cli/volumes.py`
- Modify: `src/talkub/cli/nodes.py` (register command)
- Modify: `README.md` (after the `node add` example near line 94)
- Test: `tests/cli/test_cli_volumes.py`

**Interfaces:**
- Consumes: everything above: `disks.read`, `report.disk_table`, `report.plan_text`, `selector.suggest`, `selector.Selector`, `sizes.*`, `validate.check_volume`, `validate.supports`, `simulate.simulate`, `simulate.DEFAULT_EPHEMERAL`, `render.*`, `nodes.node_patch_path`, `nodes.read_node_documents`, `nodes.upsert_documents`
- Produces: CLI `talkub node volumes NAME [--insecure | --installed]`

Dialog order (spec §3): read disks → table → install disk (maintenance mode, Talos ≥ 1.14; else a note) → EPHEMERAL (maintenance mode, no existing EPHEMERAL document) → loop "Add a volume?" → preview → confirm → write.

- [ ] **Step 1: Write the failing tests**

`tests/cli/test_cli_volumes.py`:

```python
import io
import sys

import pytest

from talkub.cli.main import main
from talkub.talos import disks
from talkub.talos.nodes import read_node_documents, render_node_patch
from talkub.talos.volumes.model import GiB, Disk, Inventory

NVME = Disk("nvme0n1", "/dev/nvme0n1", 512 * GiB, transport="nvme", serial="S1")
HDD = Disk("sda", "/dev/sda", 4096 * GiB, transport="sata", rotational=True, serial="H1")
STICK = Disk("sdb", "/dev/sdb", 32 * GiB, transport="usb", serial="U1")


def _run(argv):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    return exc.value.code


@pytest.fixture
def node_repo(empty_repo, monkeypatch):
    path = empty_repo.talos_dir / "nodes" / "cp-01.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_node_patch("cp-01"))
    calls = []

    def fake_read(ctx, name, mode):
        calls.append(mode)
        return Inventory((NVME, HDD, STICK), (), None, installed=False)

    monkeypatch.setattr(disks, "read", fake_read)
    empty_repo.read_calls = calls
    return empty_repo


def _answers(monkeypatch, *lines):
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(lines) + "\n"))


def _kinds(repo):
    return {(d["kind"], d.get("name")): d for d in read_node_documents(repo.talos_dir / "nodes" / "cp-01.yaml")}


def test_full_dialog_writes_install_ephemeral_and_volumes(node_repo, monkeypatch, capfd):
    _answers(
        monkeypatch,
        "1",  # install disk: nvme0n1
        "1",  # recommended selector
        "2",  # EPHEMERAL: limit
        "1",  # fixed size
        "40GiB",
        "n",  # do not encrypt EPHEMERAL
        "y",  # add a volume
        "local",
        "1",  # partition with filesystem
        "1",  # disk nvme0n1
        "1",  # recommended selector
        "1",  # fixed size
        "100GiB",
        "1",  # xfs
        "y",  # encrypt
        "y",  # add another volume
        "ceph",
        "2",  # raw
        "2",  # disk sda
        "1",
        "2",  # rest of the disk
        "n",  # no grow
        "n",  # no encryption
        "n",  # no more volumes
        "y",  # write
    )
    assert _run(["--repo", str(node_repo.root), "node", "volumes", "cp-01"]) == 0
    out = capfd.readouterr().out
    assert "/dev/nvme0n1 ★" in out and "u-local 100.0 GiB" in out
    docs = _kinds(node_repo)
    assert docs[("UnattendedInstallConfig", None)]["provisioning"]["diskSelector"]["match"] == (
        '!disk.readonly && !disk.cdrom && disk.transport == "nvme"'
    )
    assert docs[("VolumeConfig", "EPHEMERAL")]["provisioning"] == {"minSize": "40GiB", "maxSize": "40GiB", "grow": False}
    local = docs[("UserVolumeConfig", "local")]
    assert local["provisioning"]["diskSelector"]["match"].endswith("system_disk")
    assert local["encryption"]["keys"][0]["nodeID"] == {}
    assert docs[("RawVolumeConfig", "ceph")]["provisioning"]["minSize"] == "1GiB"
    assert ("HostnameConfig", None) in docs
    assert node_repo.read_calls == ["auto"]


def test_volume_on_unlimited_system_disk_is_rejected_and_not_written(node_repo, monkeypatch, capfd):
    _answers(
        monkeypatch,
        "1", "1",  # install on nvme
        "1",  # EPHEMERAL: whole system disk
        "y", "local", "1", "1", "1", "1", "100GiB", "1", "n",  # volume on the system disk
        "n",  # no more volumes
        "y",  # write
    )
    assert _run(["--repo", str(node_repo.root), "node", "volumes", "cp-01"]) == 0
    out = capfd.readouterr().out
    assert "✗ u-local" in out and "limit EPHEMERAL" in out and "volume not added" in out
    assert ("UserVolumeConfig", "local") not in _kinds(node_repo)


def test_usb_install_disk_warns(node_repo, monkeypatch, capfd):
    _answers(monkeypatch, "3", "1", "1", "n", "y")
    assert _run(["--repo", str(node_repo.root), "node", "volumes", "cp-01"]) == 0
    assert "USB" in capfd.readouterr().out
    match = _kinds(node_repo)[("UnattendedInstallConfig", None)]["provisioning"]["diskSelector"]["match"]
    assert match == '!disk.readonly && !disk.cdrom && disk.transport == "usb"'


def test_declining_the_write_changes_nothing(node_repo, monkeypatch):
    before = (node_repo.talos_dir / "nodes" / "cp-01.yaml").read_text()
    _answers(monkeypatch, "1", "1", "1", "n", "n")
    assert _run(["--repo", str(node_repo.root), "node", "volumes", "cp-01"]) == 1
    assert (node_repo.talos_dir / "nodes" / "cp-01.yaml").read_text() == before


def test_duplicate_volume_name_is_asked_again(node_repo, monkeypatch, capfd):
    path = node_repo.talos_dir / "nodes" / "cp-01.yaml"
    path.write_text(
        render_node_patch("cp-01")
        + "---\napiVersion: v1alpha1\nkind: UserVolumeConfig\nname: data\nprovisioning:\n"
        + "  diskSelector:\n    match: disk.transport == \"sata\"\n  minSize: 1GiB\n"
    )
    _answers(
        monkeypatch,
        "1", "1",  # install on nvme
        "2", "1", "40GiB", "n",  # EPHEMERAL limited to 40 GiB
        "y", "data",  # rejected: exists
        "data2", "1", "1", "1", "1", "10GiB", "1", "n",  # 10 GiB partition on nvme next to EPHEMERAL
        "n",  # no more volumes
        "y",  # write
    )
    assert _run(["--repo", str(node_repo.root), "node", "volumes", "cp-01"]) == 0
    assert "already exists" in capfd.readouterr().out
    names = [name for kind, name in _kinds(node_repo) if kind == "UserVolumeConfig"]
    assert names == ["data", "data2"]


def test_flags_exclude_each_other(node_repo, capfd):
    assert _run(["--repo", str(node_repo.root), "node", "volumes", "cp-01", "--insecure", "--installed"]) == 1
    assert "exclude each other" in capfd.readouterr().err


def test_unknown_node(node_repo, capfd):
    assert _run(["--repo", str(node_repo.root), "node", "volumes", "nope"]) == 1
```

Note on `test_duplicate_volume_name_is_asked_again`: the existing `data` volume (sata, min 1 GiB, no max) takes all of `sda` in the simulation, which is why `data2` goes to `nvme0n1` with EPHEMERAL limited.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/cli/test_cli_volumes.py -q`
Expected: FAIL (`No such command 'volumes'`, exit code 2)

- [ ] **Step 3: Implement**

`src/talkub/cli/volumes.py`:

```python
"""node volumes: interactive assistant for the install disk, EPHEMERAL and user volumes."""

from __future__ import annotations

import sys
from collections.abc import Sequence

import typer

from talkub.core import yamlio
from talkub.core.errors import ValidationError
from talkub.talos import disks
from talkub.talos.nodes import node_patch_path, read_node_documents, upsert_documents
from talkub.talos.volumes import render, report
from talkub.talos.volumes.model import Disk, Inventory, Kind, Size, SizeKind, VolumeSpec
from talkub.talos.volumes.selector import Selector, parse, suggest
from talkub.talos.volumes.simulate import DEFAULT_EPHEMERAL, simulate
from talkub.talos.volumes.sizes import Intent, format_bytes, parse_size, to_fields
from talkub.talos.volumes.validate import NAME, check_volume, supports

SIZE_HELP = (
    "Talos semantics: minSize only picks the disk; without maxSize a volume takes the largest free\n"
    "region; percent and negative sizes are relative to that free region, not to the disk."
)


def _menu(title: str, options: Sequence[tuple[str, str]], default: int = 1) -> str:
    """Numbered choice; returns the key of the chosen option."""
    print(title)
    for number, (_, label) in enumerate(options, start=1):
        print(f"  {number}) {label}")
    while True:
        choice = typer.prompt("choice", default=str(default))
        if choice.isdigit() and 1 <= int(choice) <= len(options):
            return options[int(choice) - 1][0]
        print(f"enter 1-{len(options)}")


def _pick_disk(title: str, inventory: Inventory) -> Disk:
    while True:
        choice = typer.prompt(f"{title} (# from the table)")
        if choice.isdigit() and 1 <= int(choice) <= len(inventory.disks):
            disk = inventory.disks[int(choice) - 1]
            if disk.readonly or disk.cdrom:
                print(f"{disk.dev_path} is read-only or a CD-ROM")
                continue
            return disk
        print(f"enter 1-{len(inventory.disks)}")


def _pick_selector(options: list[Selector], disk: Disk, *, allow_custom: bool) -> Selector:
    choices = [(str(i), s.to_cel() + ("  (recommended)" if i == 0 else "")) for i, s in enumerate(options)]
    if allow_custom:
        choices.append(("custom", "own CEL expression (not verified against the disks)"))
    if not choices:
        raise ValidationError(f"no selector matches only {disk.dev_path}", hint="the disk has no serial or WWID")
    key = _menu(f"selector for {disk.dev_path}:", choices)
    if key == "custom":
        return parse(typer.prompt("CEL expression"))
    return options[int(key)]


def _ask_size(version: str, kind: Kind) -> tuple[Size | None, Size | None, bool]:
    print(SIZE_HELP)
    intents = [(Intent.FIXED.value, "fixed size (minSize = maxSize)")]
    if kind is not Kind.EPHEMERAL:
        intents.append((Intent.REST.value, "rest of the disk (largest free region)"))
    if supports(version, "percent"):
        intents.append((Intent.PERCENT.value, "percent of the free region"))
    if supports(version, "negative") and kind is not Kind.RAW:
        intents.append((Intent.ALL_BUT.value, "all of the free region but X"))
    intent = Intent(_menu("size:", intents))
    while True:
        try:
            amount = None
            if intent is Intent.FIXED:
                amount = parse_size(typer.prompt("size, e.g. 100GiB or 100GB"))
            elif intent is Intent.PERCENT:
                amount = parse_size(typer.prompt("percent, e.g. 50%").rstrip("%") + "%")
            elif intent is Intent.ALL_BUT:
                amount = parse_size("-" + typer.prompt("leave free, e.g. 20GiB").lstrip("-"))
            if amount is not None and amount.kind is not SizeKind.PERCENT:
                print(f"  {amount.text} = {format_bytes(amount.value)}")
            grow = False
            if intent is Intent.REST:
                grow = typer.confirm("grow later into free space right behind it?", default=False)
            return to_fields(intent, amount, grow=grow)
        except ValidationError as err:
            print(f"error: {err}")


def _ask_encryption(installed: bool) -> bool:
    if installed:
        print("note: only newly created, empty volumes are encrypted")
    return typer.confirm(
        "encrypt (LUKS2, key bound to this node and its STATE; unreadable after a reset)?", default=False
    )


def _choose_install(inventory: Inventory) -> tuple[Disk, Selector]:
    print("\nInstall disk (becomes the system disk; the install wipes it)")
    disk = _pick_disk("install disk", inventory)
    if disk.transport == "usb":
        print("warning: installing to a USB disk; the selector cannot exclude USB devices")
    return disk, _pick_selector(suggest(inventory.disks, disk, None, install=True), disk, allow_custom=True)


def _ask_ephemeral(version: str) -> VolumeSpec | None:
    print("\nEPHEMERAL (/var: images, logs, emptyDir) takes the rest of the system disk unless limited.")
    print("A limit only works before the first boot from disk.")
    if _menu("EPHEMERAL:", [("whole", "whole system disk"), ("limit", "limit it")]) == "whole":
        return None
    min_size, max_size, _ = _ask_size(version, Kind.EPHEMERAL)
    spec = VolumeSpec("EPHEMERAL", Kind.EPHEMERAL, None, min_size, max_size, False, encrypted=_ask_encryption(False))
    check_volume(spec, version)
    return spec


def _ask_volume(inventory: Inventory, system: str | None, version: str, taken: set[str]) -> VolumeSpec:
    while True:
        name = typer.prompt("volume name (mounted at /var/mnt/<name>)")
        if not NAME.fullmatch(name):
            print("1 to 34 characters: letters, digits and '-'")
        elif name in taken:
            print(f"volume '{name}' already exists")
        else:
            break
    kinds = [("partition", "partition with filesystem"), ("raw", "raw partition (no filesystem, e.g. Ceph)")]
    if supports(version, "disk"):
        kinds.append(("disk", "whole disk (must be empty)"))
    kind = Kind(_menu("type:", kinds))
    disk = _pick_disk("disk", inventory)
    options = suggest(inventory.disks, disk, system, install=False)
    selector = _pick_selector(options, disk, allow_custom=kind is not Kind.DISK)
    min_size = max_size = None
    grow = False
    if kind is not Kind.DISK:
        min_size, max_size, grow = _ask_size(version, kind)
    filesystem = "xfs"
    if kind is not Kind.RAW:
        systems = [("xfs", "xfs"), ("ext4", "ext4")] + ([("btrfs", "btrfs")] if supports(version, "btrfs") else [])
        filesystem = _menu("filesystem:", systems)
    spec = VolumeSpec(name, kind, selector, min_size, max_size, grow, filesystem, _ask_encryption(inventory.installed))
    check_volume(spec, version)
    return spec


def volumes(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Node name."),
    insecure: bool = typer.Option(False, "--insecure", help="Read disks via the maintenance API (not installed)."),
    installed: bool = typer.Option(False, "--installed", help="Read disks via the talosconfig (installed node)."),
) -> None:
    """Choose install disk, EPHEMERAL size and volumes from the node's disks; writes talos/nodes/<name>.yaml."""
    if insecure and installed:
        raise ValidationError("--insecure and --installed exclude each other")
    context = ctx.obj.context()
    settings = context.repo.load_settings()
    settings.node(name)
    version = settings.talos_version
    path = node_patch_path(context.repo, name)
    if not path.is_file():
        raise ValidationError(f"{context.repo.rel(path)} does not exist", hint=f"create it with `talkub node add {name} …`")
    mode = "insecure" if insecure else "installed" if installed else "auto"
    inventory = disks.read(context, name, mode)
    print(f"disks of {name} ({'installed' if inventory.installed else 'maintenance mode'}):")
    print(report.disk_table(inventory, inventory.system_disk))

    existing_docs = read_node_documents(path)
    new_docs: list[dict] = []
    system, install_disk = inventory.system_disk, None
    if not inventory.installed:
        if supports(version, "install"):
            disk, selector = _choose_install(inventory)
            system = install_disk = disk.id
            current = next((d for d in existing_docs if d.get("kind") == "UnattendedInstallConfig"), None)
            new_docs.append(render.install_document(selector, current))
        else:
            print(f"note: talosVersion {version} has no UnattendedInstallConfig; the install disk is not chosen here")

    specs = [s for s in (render.spec_from_document(d) for d in existing_docs) if s is not None]
    ephemeral = next((s for s in specs if s.kind is Kind.EPHEMERAL), None)
    others = [s for s in specs if s.kind is not Kind.EPHEMERAL]
    if inventory.installed:
        print("note: EPHEMERAL is already provisioned; resizing it needs a wipe of EPHEMERAL")
    elif ephemeral is None:
        ephemeral = _ask_ephemeral(version)
        if ephemeral is not None:
            new_docs.append(render.volume_document(ephemeral))

    added: list[VolumeSpec] = []

    def planned(extra: list[VolumeSpec]):
        return simulate(
            inventory,
            [ephemeral or DEFAULT_EPHEMERAL, *others, *added, *extra],
            system_disk=system,
            install_disk=install_disk,
        )

    print("\n" + report.plan_text(planned([]), inventory, system))
    while typer.confirm("\nadd a volume?", default=False):
        try:
            spec = _ask_volume(inventory, system, version, {s.name for s in [*others, *added]})
        except ValidationError as err:
            print(f"error: {err}" + (f" ({err.hint})" if err.hint else ""))
            continue
        plan = planned([spec])
        print(report.plan_text(plan, inventory, system))
        if spec.volume_id in plan.errors:
            print("volume not added")
            continue
        if plan.warnings and not typer.confirm("keep this volume?", default=True):
            continue
        added.append(spec)
    new_docs += [render.volume_document(spec) for spec in added]

    if not new_docs:
        print("nothing to write")
        return
    print("\n---\n".join(yamlio.dump(doc) for doc in new_docs))
    if not typer.confirm(f"write these documents to {context.repo.rel(path)}?", default=True):
        print("aborted, nothing written", file=sys.stderr)
        raise typer.Exit(1)
    upsert_documents(path, new_docs)
    print(f"written: {context.repo.rel(path)}")
    step = f"talkub plan {name}" if inventory.installed else f"talkub apply --insecure {name}"
    print(f"next: `{step}`")
```

Register in `src/talkub/cli/nodes.py` (below the `add` command):

```python
from talkub.cli.volumes import volumes as volumes_command  # at the top with the other imports

app.command("volumes")(volumes_command)
```

README: after the `node add` block near line 94 add:

```markdown
Once the node is booted (maintenance mode), `talkub node volumes w-01` lists its disks and asks for
the install disk, an optional EPHEMERAL limit and user volumes (partition, raw or whole disk,
optionally LUKS2 with a node-bound key). It suggests the most portable CEL selector that matches
exactly the chosen disk, simulates where Talos will place each volume and how large it becomes,
and writes the documents into `talos/nodes/w-01.yaml`. On an installed node it reads the disks via
the talosconfig and only adds volumes.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/cli/test_cli_volumes.py -q`
Expected: PASS. When a dialog test fails with an `Abort` (stdin exhausted), print the captured output — it shows the prompt that had no answer; fix the answer list, not the prompt order in the spec.

- [ ] **Step 5: Full check and commit**

Run: `uv run pytest -q && uv run ruff check && uv run ruff format --check && uv run lint-imports && uv run basedpyright src/talkub/talos/volumes src/talkub/talos/disks.py src/talkub/cli/volumes.py`
Expected: all green (basedpyright: no new errors in these files)

```bash
git add src/talkub/cli/volumes.py src/talkub/cli/nodes.py README.md tests/cli/test_cli_volumes.py
git commit -m "feat(cli): node volumes assistant"
```
