"""Parsing talosctl / kubectl output. Unexpected input gives None / "unknown", never an exception.

The talosctl text formats (version, apply-config --dry-run) follow the talosctl docs; keep
all format knowledge here so it can be adjusted in one place.
"""

from __future__ import annotations

import json
import re
from collections import Counter

_SERVER_TAG = re.compile(r"^Server:\s*$.*?^\s*Tag:\s*(\S+)", re.MULTILINE | re.DOTALL)


def server_version(text: str) -> str | None:
    """Talos version of the node from `talosctl version` output."""
    match = _SERVER_TAG.search(text)
    return match.group(1) if match else None


def json_documents(text: str) -> list[object]:
    """Parse a stream of concatenated JSON documents; stops at the first invalid one."""
    decoder = json.JSONDecoder()
    docs: list[object] = []
    index = 0
    while True:
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            return docs
        try:
            doc, index = decoder.raw_decode(text, index)
        except ValueError:
            return docs
        docs.append(doc)


def extensions(text: str) -> dict[str, str]:
    """name -> version from `talosctl get extensions -o json`."""
    result: dict[str, str] = {}
    for doc in json_documents(text):
        if not isinstance(doc, dict):
            continue
        spec = doc.get("spec")
        metadata = spec.get("metadata") if isinstance(spec, dict) else None
        if isinstance(metadata, dict) and isinstance(metadata.get("name"), str):
            result[metadata["name"]] = str(metadata.get("version", ""))
    return result


def schematic_id(text: str) -> str | None:
    """Image Factory images report their schematic ID as the `schematic` extension version."""
    return extensions(text).get("schematic") or None


def config_drift(text: str) -> str:
    """Classify `talosctl apply-config --dry-run` output."""
    lowered = text.lower()
    if "no changes" in lowered:
        return "in sync"
    if "config diff" in lowered:
        return "changes"
    return "unknown"


def _items(text: str) -> list[dict]:
    docs = json_documents(text)
    if len(docs) != 1 or not isinstance(docs[0], dict) or not isinstance(docs[0].get("items"), list):
        return []
    return [item for item in docs[0]["items"] if isinstance(item, dict)]


def kubelet_versions(text: str) -> dict[str, str]:
    """node name -> kubelet version (without leading v) from `kubectl get nodes -o json`."""
    result: dict[str, str] = {}
    for item in _items(text):
        name = (item.get("metadata") or {}).get("name")
        version = ((item.get("status") or {}).get("nodeInfo") or {}).get("kubeletVersion")
        if isinstance(name, str) and isinstance(version, str):
            result[name] = version.lstrip("v")
    return result


def application_summary(text: str) -> dict[str, int] | None:
    """'<sync>/<health>' -> count from `kubectl get applications -o json`; None if not parseable."""
    docs = json_documents(text)
    if len(docs) != 1 or not isinstance(docs[0], dict) or not isinstance(docs[0].get("items"), list):
        return None
    counts: Counter[str] = Counter()
    for item in _items(text):
        status = item.get("status") or {}
        sync = (status.get("sync") or {}).get("status") or "Unknown"
        health = (status.get("health") or {}).get("status") or "Unknown"
        counts[f"{sync}/{health}"] += 1
    return dict(counts)
