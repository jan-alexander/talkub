"""talkub rotate-ca: rotate the Talos API CA and bring the repo in line."""

from __future__ import annotations

from io import StringIO

from ruamel.yaml import YAML

from talkub.errors import ValidationError


def _bundle_yaml() -> YAML:
    yaml = YAML()
    yaml.indent(mapping=4, sequence=4, offset=2)
    yaml.width = 1 << 20  # no line wrapping: base64 PEM values can exceed 4096 chars
    return yaml


def replace_os_ca(bundle: bytes, crt: str, key: str) -> bytes:
    """The secrets bundle with certs.os replaced; everything else round-trips unchanged."""
    yaml = _bundle_yaml()
    doc = yaml.load(bundle.decode())
    os_certs = doc.get("certs", {}).get("os") if isinstance(doc, dict) else None
    if not isinstance(os_certs, dict):
        raise ValidationError("the Talos secrets bundle has no certs.os")
    os_certs["crt"] = crt
    os_certs["key"] = key
    buffer = StringIO()
    yaml.dump(doc, buffer)
    return buffer.getvalue().encode()
