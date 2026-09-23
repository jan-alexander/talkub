"""keys/recipients.txt: the admins' age public keys.

Format (compatible with `age -R`, which ignores comment lines):

    # name: alice
    age1...
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from talkub.errors import ValidationError

HEADER = "# talkub admin recipients - managed by `talkub admin`, readable by `age -R`"
_NAME_LINE = re.compile(r"^#\s*name:\s*(\S+)\s*$")
_KEY = re.compile(r"age1[0-9a-z]+")
_NAME = re.compile(r"[A-Za-z0-9._-]+")


@dataclass(frozen=True)
class Recipient:
    name: str
    key: str


def validate_key(key: str) -> str:
    key = key.strip()
    if not _KEY.fullmatch(key):
        raise ValidationError(f"not an age recipient: {key!r}", hint="age public keys start with 'age1'")
    return key


def validate_name(name: str) -> str:
    if not _NAME.fullmatch(name):
        raise ValidationError(
            f"invalid recipient name {name!r}", hint="use letters, digits, '.', '_' or '-'"
        )
    return name


def parse(text: str) -> list[Recipient]:
    result: list[Recipient] = []
    pending: str | None = None
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if match := _NAME_LINE.match(line):
            pending = match.group(1)
            continue
        if line.startswith("#"):
            continue
        result.append(Recipient(pending or f"unnamed-{lineno}", validate_key(line)))
        pending = None
    return result


def render(items: Sequence[Recipient]) -> str:
    lines = [HEADER]
    for item in items:
        lines += [f"# name: {validate_name(item.name)}", validate_key(item.key)]
    return "\n".join(lines) + "\n"
