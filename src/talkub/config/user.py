"""The admin's own talkub settings outside any cluster repo: the global shortcut pool in
$XDG_CONFIG_HOME/talkub/shortcuts.yaml (default ~/.config/talkub/shortcuts.yaml).

Only `talkub run` reads this file, so a broken pool never blocks other commands.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from pathlib import Path

from ruamel.yaml.error import YAMLError

from talkub.config.fields import MARKER
from talkub.config.shortcuts import Shortcut, shortcut_from_dict, shortcut_to_dict
from talkub.core import yamlio
from talkub.core.errors import ValidationError
from talkub.core.fsutil import atomic_write


def user_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "talkub"


def global_shortcuts_file() -> Path:
    return user_config_dir() / "shortcuts.yaml"


def display_path(path: Path) -> str:
    """`path` for messages, with the home directory shown as ~."""
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)


def _read(path: Path) -> object:
    """The file's YAML document (round-trip, keeps comments); None if the file does not exist."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    try:
        return yamlio.load(text)
    except YAMLError as err:
        raise ValidationError(f"{display_path(path)} is not valid YAML: {err}") from err


def _parse(data: object, where: str) -> dict[str, Shortcut]:
    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise ValidationError(f"{where} must be a mapping")
    raw = data.get("shortcuts")
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValidationError(f"{where}: 'shortcuts' must be a mapping")
    shortcuts: dict[str, Shortcut] = {}
    for name, spec in raw.items():
        try:
            shortcuts[str(name)] = shortcut_from_dict(str(name), spec)
        except ValidationError as err:
            # The field helpers always name .talkub.yaml; point at the global file instead.
            message = str(err).removeprefix(f"{MARKER}: ")
            raise ValidationError(f"{where}: {message}", hint=err.hint) from None
    return shortcuts


def load_global_shortcuts() -> dict[str, Shortcut]:
    path = global_shortcuts_file()
    return _parse(_read(path), display_path(path))


def save_global_shortcut(shortcut: Shortcut, *, force: bool = False) -> Path:
    """Add `shortcut` to the pool, keeping the file's comments and other entries. The whole file
    is validated first, so a broken pool is never overwritten."""
    path = global_shortcuts_file()
    where = display_path(path)
    data = _read(path)
    if shortcut.name in _parse(data, where) and not force:
        raise ValidationError(
            f"global shortcut {shortcut.name!r} already exists in {where}",
            hint="--force replaces it",
        )
    root: MutableMapping = data if isinstance(data, MutableMapping) else {}
    if root.get("shortcuts") is None:
        root["shortcuts"] = {}
    root["shortcuts"][shortcut.name] = shortcut_to_dict(shortcut)
    atomic_write(path, yamlio.dump(root).encode())
    return path
