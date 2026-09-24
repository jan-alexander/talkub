"""YAML helpers based on ruamel.yaml (round-trip mode keeps comments)."""

from __future__ import annotations

from io import StringIO
from typing import Any

from ruamel.yaml import YAML


def _yaml() -> YAML:
    yaml = YAML()
    yaml.default_flow_style = False
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def load(text: str) -> Any:
    return _yaml().load(text)


def load_all(text: str) -> list[Any]:
    return list(_yaml().load_all(text))


def dump(data: Any) -> str:
    buffer = StringIO()
    _yaml().dump(data, buffer)
    return buffer.getvalue()


def dump_all(docs: list[Any]) -> str:
    buffer = StringIO()
    _yaml().dump_all(docs, buffer)
    return buffer.getvalue()
