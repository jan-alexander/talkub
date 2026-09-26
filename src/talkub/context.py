"""Everything a command needs to work on one cluster repo."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from talkub.crypto.sops import Sops
from talkub.keyring import Keyring
from talkub.repo import Repo
from talkub.runner import Runner


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
