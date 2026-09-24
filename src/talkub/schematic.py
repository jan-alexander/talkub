"""Talos Image Factory schematics: the customization talkub sends (pure data, no I/O)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from talkub import yamlio

FACTORY_HOST = "factory.talos.dev"


def customization(extensions: Sequence[str], kernel_args: Sequence[str]) -> dict:
    """The `customization` block of a schematic; empty lists are omitted like the factory does."""
    custom: dict = {}
    if kernel_args:
        custom["extraKernelArgs"] = list(kernel_args)
    if extensions:
        custom["systemExtensions"] = {"officialExtensions": sorted(extensions)}
    return custom


def customization_hash(extensions: Sequence[str], kernel_args: Sequence[str]) -> str:
    data = json.dumps(customization(extensions, kernel_args), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()


def schematic_document(extensions: Sequence[str], kernel_args: Sequence[str]) -> str:
    """Request body for POST /schematics."""
    return yamlio.dump({"customization": customization(extensions, kernel_args)})
