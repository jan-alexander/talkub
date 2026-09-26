import os

import pytest

from talkub.core.fsutil import atomic_write


def test_atomic_write_creates_parents_and_sets_mode(tmp_path):
    target = tmp_path / "a" / "b" / "file.txt"
    atomic_write(target, b"hello", mode=0o600)
    assert target.read_bytes() == b"hello"
    assert target.stat().st_mode & 0o777 == 0o600


def test_atomic_write_keeps_original_on_failure(tmp_path, monkeypatch):
    target = tmp_path / "file.txt"
    target.write_bytes(b"original")

    def broken_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(OSError):
        atomic_write(target, b"new")
    assert target.read_bytes() == b"original"
    assert [p.name for p in tmp_path.iterdir()] == ["file.txt"]
