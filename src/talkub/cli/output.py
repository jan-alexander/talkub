"""Console output shared by CLI commands."""

from __future__ import annotations


def print_notes(notes: list[str]) -> None:
    for note in notes:
        print(f"note: {note}")
