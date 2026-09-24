"""Parsing talosctl / kubectl output. Unexpected input gives None / "unknown", never an exception.

The talosctl text formats (version, apply-config --dry-run) follow the talosctl docs; keep
all format knowledge here so it can be adjusted in one place.
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections import Counter

_SERVER_TAG = re.compile(r"^Server:\s*$.*?^\s*Tag:\s*(\S+)", re.MULTILINE | re.DOTALL)
_TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")


def _dict(value: object) -> dict:
    """Return value if it is a dict, else empty dict. Guards against non-dict nested lookups."""
    return value if isinstance(value, dict) else {}


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
        spec = _dict(doc.get("spec"))
        metadata = _dict(spec.get("metadata"))
        if isinstance(metadata.get("name"), str):
            result[metadata["name"]] = str(metadata.get("version", ""))
    return result


def schematic_id(text: str) -> str | None:
    """Image Factory images report their schematic ID as the `schematic` extension version."""
    return extensions(text).get("schematic") or None


def bootstrap_already_done(stderr: str) -> bool:
    """Whether `talosctl bootstrap` stderr says etcd was already bootstrapped."""
    return "AlreadyExists" in stderr or "already bootstrapped" in stderr.lower()


def etcd_member_count(text: str) -> int | None:
    """Number of members in the `talosctl etcd members` table; None if there is no table.

    The table starts with a header line beginning with "NODE"; every non-empty line after it
    is one member.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines or not lines[0].lstrip().startswith("NODE"):
        return None
    return len(lines) - 1


def tailnet_ipv4(text: str) -> str | None:
    """First tailnet IPv4 (100.64.0.0/10) ADDRESS in `talosctl get addresses` table output.

    The ADDRESS column is located via the header, like `etcd_member_count`'s NODE check;
    the prefix length (e.g. /32) is stripped.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines or not lines[0].lstrip().startswith("NODE"):
        return None
    header = lines[0].split()
    try:
        index = header.index("ADDRESS")
    except ValueError:
        return None
    for line in lines[1:]:
        fields = line.split()
        if len(fields) <= index:
            continue
        try:
            address = ipaddress.ip_address(fields[index].split("/", 1)[0])
        except ValueError:
            continue
        if address in _TAILNET_V4:
            return str(address)
    return None


_YAML_KV = re.compile(r"^(?P<head>.*?)(?P<key>[A-Za-z][\w]*):(?P<sep>[ \t]+)(?P<value>\S.*)$")
_ENV_KV = re.compile(r"(?P<key>[A-Za-z_][\w]*)=(?P<value>[^\s\"']+)")
_SENSITIVE_ENV_HINT = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD)", re.IGNORECASE)
_SENSITIVE_KEY_SUFFIXES = ("key", "secret", "token")


def _is_sensitive_key(name: str) -> bool:
    return name.lower().endswith(_SENSITIVE_KEY_SUFFIXES)


def _redact_line(line: str) -> str:
    match = _YAML_KV.match(line)
    if match:
        if _is_sensitive_key(match.group("key")):
            return f"{match.group('head')}{match.group('key')}:{match.group('sep')}<redacted>"
        return line

    def replace(env_match: re.Match) -> str:
        if _SENSITIVE_ENV_HINT.search(env_match.group("key")):
            return f"{env_match.group('key')}=<redacted>"
        return env_match.group(0)

    return _ENV_KV.sub(replace, line)


def redact_diff(text: str) -> str:
    """Redact secret values in a `talosctl apply-config --dry-run` diff, keeping diff markers
    and indentation intact.

    Redacts the value of any YAML mapping line whose key ends with key/secret/token
    (case-insensitive; `crt:` / certificates are public and stay visible), and list entries
    shaped `KEY=value` whose KEY contains key/token/secret/password (e.g. `TS_AUTHKEY=...`).
    """
    return "\n".join(_redact_line(line) for line in text.split("\n"))


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
        metadata = _dict(item.get("metadata"))
        status = _dict(item.get("status"))
        nodeInfo = _dict(status.get("nodeInfo"))
        name = metadata.get("name")
        version = nodeInfo.get("kubeletVersion")
        if isinstance(name, str) and isinstance(version, str):
            result[name] = version.lstrip("v")
    return result


def application_summary(text: str) -> dict[str, int] | None:
    """'<sync>/<health>' -> count from `kubectl get applications -o json`; None if not parseable."""
    docs = json_documents(text)
    if len(docs) != 1 or not isinstance(docs[0], dict) or not isinstance(docs[0].get("items"), list):
        return None
    counts: Counter[str] = Counter()
    items = docs[0].get("items", [])
    for item in items:
        if not isinstance(item, dict):
            continue
        status = _dict(item.get("status"))
        sync = _dict(status.get("sync"))
        health = _dict(status.get("health"))
        sync_status = sync.get("status") or "Unknown"
        health_status = health.get("status") or "Unknown"
        counts[f"{sync_status}/{health_status}"] += 1
    return dict(counts)
