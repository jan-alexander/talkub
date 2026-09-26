"""Everything a command needs to work on one cluster repo."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from talkub.config import Repo
from talkub.core.runner import Runner
from talkub.secrets.keyring import Keyring
from talkub.secrets.sops import Sops


@dataclass
class Context:
    repo: Repo
    runner: Runner
    keyring: Keyring
    sops: Sops

    @classmethod
    def open(
        cls,
        repo: Repo,
        runner: Runner | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Context:
        runner = runner or Runner()
        keyring = Keyring(repo, runner, env)
        return cls(
            repo=repo,
            runner=runner,
            keyring=keyring,
            sops=Sops(runner, keyring.master_key),
        )
