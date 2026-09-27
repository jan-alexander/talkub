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

from talkub.talos.volumes.model import Disk, GiB


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
        selector = Selector((*_exclusions(target, identifying), *identifying, *system_terms))
        hits = [d for d in disks if selector.matches(d, is_system(d, system_disk, install=install))]
        if hits == [target] and selector not in found:
            found.append(selector)
    return found
