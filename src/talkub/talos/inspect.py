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
    """Tailnet IPv4 (100.64.0.0/10) ADDRESS in `talosctl get addresses` table output.

    The ADDRESS (and, if present, LINK) columns are located via the header, like
    `etcd_member_count`'s NODE check; the prefix length (e.g. /32) is stripped. The row whose
    LINK is `tailscale0` is preferred; other interfaces can carry a CGNAT-range address too and
    may be listed first, so the first tailnet-range address is only a fallback.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines or not lines[0].lstrip().startswith("NODE"):
        return None
    header = lines[0].split()
    try:
        address_index = header.index("ADDRESS")
    except ValueError:
        return None
    link_index = header.index("LINK") if "LINK" in header else None
    fallback: str | None = None
    for line in lines[1:]:
        fields = line.split()
        if len(fields) <= address_index:
            continue
        try:
            address = ipaddress.ip_address(fields[address_index].split("/", 1)[0])
        except ValueError:
            continue
        if address not in _TAILNET_V4:
            continue
        if link_index is not None and len(fields) > link_index and fields[link_index] == "tailscale0":
            return str(address)
        if fallback is None:
            fallback = str(address)
    return fallback


_MARKER_CHARS = (" ", "+", "-")
_YAML_KV = re.compile(r"^(?P<indent>[ \t]*)(?P<key>[A-Za-z][\w]*):(?P<sep>[ \t]+)(?P<value>\S.*)$")
_BLOCK_SCALAR = re.compile(r"^[|>][+\-0-9]*\s*$")
_SENSITIVE_KEY_SUFFIXES = ("key", "secret", "token", "password", "auth")

# `NAME=value` / `--name=value`, anywhere in a line: unquoted (stops at whitespace or a YAML flow
# delimiter), single- or double-quoted. Applied recursively so a secret nested inside another
# value (`TS_EXTRA_ARGS=--authkey=x`) is still found.
_ENV_ASSIGN = re.compile(
    r"(?P<prefix>--)?(?P<key>[A-Za-z_][\w-]*)="
    r"(?:'(?P<sq>[^']*)'|\"(?P<dq>[^\"]*)\"|(?P<bare>[^\s,}\]'\"]*))"
)
_SENSITIVE_ENV_HINT = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD)", re.IGNORECASE)

# `key: value` pairs inside a YAML flow mapping (`{crt: X, key: Y}`); single level, as produced
# by talosctl's dry-run diff.
_BRACE_SPAN = re.compile(r"\{[^{}]*\}")
_FLOW_KV = re.compile(r"(?P<key>[A-Za-z][\w]*):\s*(?P<value>[^,{}]+)")


def _is_sensitive_key(name: str) -> bool:
    return name.lower().endswith(_SENSITIVE_KEY_SUFFIXES)


def _split_marker(line: str) -> tuple[str, str]:
    """(diff-marker column, rest of the line). Real talosctl diff lines reserve column 1 for
    ' '/'+'/'-'; when present it is excluded from indentation comparisons."""
    if line[:1] in _MARKER_CHARS:
        return line[0], line[1:]
    return "", line


def _redact_env_assignments(text: str) -> str:
    def replace(match: re.Match) -> str:
        key = match.group("key")
        if match.group("sq") is not None:
            value, quote = match.group("sq"), "'"
        elif match.group("dq") is not None:
            value, quote = match.group("dq"), '"'
        else:
            value, quote = match.group("bare"), ""
        if not value:
            return match.group(0)
        prefix = match.group("prefix") or ""
        if _SENSITIVE_ENV_HINT.search(key):
            return f"{prefix}{key}={quote}<redacted>{quote}"
        return f"{prefix}{key}={quote}{_redact_env_assignments(value)}{quote}"

    return _ENV_ASSIGN.sub(replace, text)


def _redact_flow_mappings(text: str) -> str:
    def replace_pair(match: re.Match) -> str:
        if _is_sensitive_key(match.group("key")):
            return f"{match.group('key')}: <redacted>"
        return match.group(0)

    def replace_span(match: re.Match) -> str:
        return _FLOW_KV.sub(replace_pair, match.group(0))

    return _BRACE_SPAN.sub(replace_span, text)


def _redact_nested(content: str) -> str:
    """Secrets that are not a top-level `sensitive-key: value` line: flow-mapping entries and
    `NAME=value`/`--name=value` occurrences anywhere (list items, flow sequences, quoted)."""
    return _redact_env_assignments(_redact_flow_mappings(content))


def redact_diff(text: str) -> str:
    """Redact secret values in a `talosctl apply-config --dry-run` diff, keeping diff markers
    and indentation intact.

    Redacts: the value of any YAML mapping line whose key ends with key/secret/token/password/auth
    (case-insensitive; `crt:` / certificates are public and stay visible) — including every line
    of a block-scalar value (`key: |`) until the indentation returns to the key's own level;
    `key: value` pairs nested inside a flow mapping (`{crt: X, key: Y}`); and `NAME=value` /
    `--name=value` occurrences anywhere in a line (list items, flow sequences, quoted values,
    values nested inside another value) whose NAME contains key/token/secret/password.
    """
    lines = text.split("\n")
    out: list[str] = []
    block_indent: int | None = None
    for line in lines:
        marker, content = _split_marker(line)
        if block_indent is not None:
            if not content.strip():
                out.append(line)  # blank line inside the block scalar: nothing to leak
                continue
            indent = len(content) - len(content.lstrip(" "))
            if indent > block_indent:
                out.append(f"{marker}{content[:indent]}<redacted>")
                continue
            block_indent = None

        match = _YAML_KV.match(content)
        if match and _is_sensitive_key(match.group("key")):
            value = match.group("value")
            if _BLOCK_SCALAR.match(value):
                block_indent = len(match.group("indent"))
            out.append(f"{marker}{match.group('indent')}{match.group('key')}:{match.group('sep')}<redacted>")
            continue
        out.append(f"{marker}{_redact_nested(content)}")
    return "\n".join(out)


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
