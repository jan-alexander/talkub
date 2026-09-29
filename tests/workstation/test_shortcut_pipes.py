"""Listing of shortcuts with pipe stages (no external tools)."""

from __future__ import annotations

from talkub.config import Shortcut
from talkub.workstation import shortcuts


def test_listing_marks_piped_kinds():
    ext = Shortcut(name="ext", kind="run", argv=("t", "get", "extensions"), pipe=(("sort",),))
    pods = Shortcut(name="pods", kind="run", description="pods", argv=("k", "get", "pods"))
    lines = shortcuts.listing({"ext": (ext, "global"), "pods": (pods, "project")})
    assert [line.split() for line in lines] == [
        ["ext", "run", "|", "global"],
        ["pods", "run", "pods", "project"],
    ]
    assert lines[0].index("global") == lines[1].index("project")
