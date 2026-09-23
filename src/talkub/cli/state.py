"""Global CLI options shared by all commands."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from talkub.context import Context
from talkub.repo import Repo, find_repo


@dataclass
class State:
    repo_path: Path | None = None
    debug: bool = False

    def repo(self) -> Repo:
        return find_repo(Path.cwd(), self.repo_path)

    def context(self) -> Context:
        return Context.open(self.repo())
