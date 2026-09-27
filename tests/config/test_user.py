"""The global shortcut pool in $XDG_CONFIG_HOME/talkub/shortcuts.yaml."""

from __future__ import annotations

import pytest

from talkub.config import Shortcut, user
from talkub.core.errors import ValidationError

PODS = Shortcut(name="pods", kind="run", description="all pods", argv=("k", "get", "pods", "-A"))


@pytest.fixture
def global_file(tmp_path):
    path = tmp_path / "xdg" / "talkub" / "shortcuts.yaml"
    path.parent.mkdir(parents=True)
    return path


def test_config_dir_follows_xdg(tmp_path):
    assert user.user_config_dir() == tmp_path / "xdg" / "talkub"
    assert user.global_shortcuts_file() == tmp_path / "xdg" / "talkub" / "shortcuts.yaml"


@pytest.mark.parametrize("xdg", [None, ""])
def test_config_dir_defaults_to_dot_config(monkeypatch, tmp_path, xdg):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    if xdg is None:
        monkeypatch.delenv("XDG_CONFIG_HOME")
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", xdg)
    assert user.user_config_dir() == tmp_path / "home" / ".config" / "talkub"


def test_display_path_abbreviates_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert user.display_path(tmp_path / "home" / ".config" / "x.yaml") == "~/.config/x.yaml"
    assert user.display_path(tmp_path / "elsewhere.yaml") == str(tmp_path / "elsewhere.yaml")


def test_missing_empty_or_blockless_file_is_an_empty_pool(global_file):
    assert user.load_global_shortcuts() == {}
    global_file.write_text("")
    assert user.load_global_shortcuts() == {}
    global_file.write_text("shortcuts:\n")
    assert user.load_global_shortcuts() == {}


def test_load(global_file):
    global_file.write_text("shortcuts:\n  pods:\n    description: all pods\n    run: [k, get, pods, -A]\n")
    assert user.load_global_shortcuts() == {"pods": PODS}


def test_invalid_entry_names_the_global_file(global_file):
    global_file.write_text("shortcuts:\n  charts:\n    run: [helm, ls]\n")
    with pytest.raises(ValidationError) as exc:
        user.load_global_shortcuts()
    message = str(exc.value)
    assert message.startswith(f"{user.display_path(global_file)}: shortcuts.charts.run must start with")
    assert ".talkub.yaml" not in message


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("shortcuts: [\n", "is not valid YAML"),
        ("- pods\n", "must be a mapping"),
        ("shortcuts: [pods]\n", "'shortcuts' must be a mapping"),
    ],
)
def test_invalid_file_structure(global_file, text, expected):
    global_file.write_text(text)
    with pytest.raises(ValidationError) as exc:
        user.load_global_shortcuts()
    assert str(exc.value).startswith(user.display_path(global_file))
    assert expected in str(exc.value)


def test_save_creates_directory_and_file(tmp_path):
    path = user.save_global_shortcut(PODS)
    assert path == tmp_path / "xdg" / "talkub" / "shortcuts.yaml"
    assert user.load_global_shortcuts() == {"pods": PODS}


def test_save_keeps_comments_and_other_entries(global_file):
    global_file.write_text("# my pool\nshortcuts:\n  nodes:  # talos view\n    run: [t, get, members]\n")
    user.save_global_shortcut(PODS)
    text = global_file.read_text()
    assert "# my pool" in text and "# talos view" in text
    assert set(user.load_global_shortcuts()) == {"nodes", "pods"}


def test_save_into_file_without_block(global_file):
    global_file.write_text("# nothing yet\n")
    user.save_global_shortcut(PODS)
    assert user.load_global_shortcuts() == {"pods": PODS}


def test_save_conflict_without_force_changes_nothing(global_file):
    global_file.write_text("shortcuts:\n  pods:\n    run: [k, get, pods]\n")
    before = global_file.read_bytes()
    with pytest.raises(ValidationError) as exc:
        user.save_global_shortcut(PODS)
    assert "global shortcut 'pods' already exists" in str(exc.value)
    assert exc.value.hint == "--force replaces it"
    assert global_file.read_bytes() == before


def test_save_with_force_replaces(global_file):
    global_file.write_text("shortcuts:\n  pods:\n    run: [k, get, pods]\n")
    user.save_global_shortcut(PODS, force=True)
    assert user.load_global_shortcuts() == {"pods": PODS}


def test_save_refuses_to_touch_a_broken_file(global_file):
    global_file.write_text("shortcuts:\n  charts:\n    run: [helm, ls]\n")
    before = global_file.read_bytes()
    with pytest.raises(ValidationError):
        user.save_global_shortcut(PODS, force=True)
    assert global_file.read_bytes() == before
