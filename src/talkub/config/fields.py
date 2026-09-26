"""Validation helpers for .talkub.yaml fields, shared by the spec parsers."""

from __future__ import annotations

import re

from talkub.core.errors import ValidationError

# DNS-1123 label: lowercase letters, digits and '-', max 63 characters, not starting/ending with '-'.
DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")

# The settings file that marks a cluster repo; validation messages name it.
MARKER = ".talkub.yaml"


def valid_label(value: object) -> bool:
    """True if `value` is a DNS-1123 label (used for shortcut/expose names, namespaces, services, ports)."""
    return isinstance(value, str) and len(value) <= 63 and bool(DNS_LABEL.match(value))


def check_choice(value: object, choices: tuple[str, ...], where: str) -> str:
    if value not in choices:
        raise ValidationError(
            f"{MARKER}: {where} has invalid value {value!r}",
            hint=f"use one of: {', '.join(choices)}",
        )
    return str(value)


def optional_text(value: object, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{MARKER}: {where} must be a non-empty string")
    return value.strip()


def text_list(value: object, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValidationError(f"{MARKER}: {where} must be a list of strings")
    return [str(item) for item in value]


def label(value: object, where: str) -> str:
    if not valid_label(value):
        raise ValidationError(f"{MARKER}: {where} must be a valid DNS label")
    return str(value)
