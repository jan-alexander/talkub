"""The cluster master key: keys/master.age, keys/master.pub and keys/recipients.txt.

All SOPS files are encrypted to the master key only. The master key itself is
encrypted to every admin recipient, so adding or removing an admin rewrites a
single small file instead of every SOPS file.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence

from talkub import identity
from talkub import recipients as recipients_file
from talkub.crypto import age
from talkub.errors import NoIdentity, ValidationError
from talkub.fsutil import atomic_write
from talkub.recipients import Recipient
from talkub.repo import Repo
from talkub.runner import Runner


class Keyring:
    def __init__(self, repo: Repo, runner: Runner, env: Mapping[str, str] | None = None) -> None:
        self.repo = repo
        self.runner = runner
        self.env = dict(os.environ if env is None else env)
        self._master: str | None = None

    def recipients(self) -> list[Recipient]:
        path = self.repo.recipients_file
        if not path.is_file():
            raise ValidationError(f"{self.repo.rel(path)} is missing")
        return recipients_file.parse(path.read_text())

    def public_key(self) -> str:
        return self.repo.master_pub.read_text().strip()

    def master_key(self) -> str:
        if self._master is None:
            self._master = self._resolve()
        return self._master

    def _resolve(self) -> str:
        if key := identity.master_key_from_env(self.env):
            return key
        files = identity.identity_files(self.env)
        if not files:
            raise NoIdentity(
                "no age identity found",
                hint=(
                    "create one with `talkub identity new` "
                    f"(stored in {identity.identities_dir(self.env)}) or set TALKUB_IDENTITY"
                ),
            )
        return age.decrypt(self.runner, self.repo.master_age, files).decode().strip()

    def create(self, recipients: Sequence[Recipient]) -> str:
        if self.repo.master_age.exists():
            raise ValidationError(f"{self.repo.rel(self.repo.master_age)} already exists")
        if not recipients:
            raise ValidationError("at least one recipient is required")
        pair = age.generate(self.runner)
        self._write(pair.secret, list(recipients))
        atomic_write(self.repo.master_pub, (pair.public + "\n").encode())
        self._master = pair.secret
        return pair.secret

    def add(self, recipient: Recipient) -> None:
        current = self.recipients()
        for existing in current:
            if existing.name == recipient.name:
                raise ValidationError(f"recipient '{recipient.name}' already exists")
            if existing.key == recipient.key:
                raise ValidationError(f"this key is already registered as '{existing.name}'")
        self._write(self.master_key(), [*current, recipient])

    def remove(self, name: str) -> None:
        current = self.recipients()
        remaining = [r for r in current if r.name != name]
        if len(remaining) == len(current):
            raise ValidationError(f"no recipient named '{name}'")
        if not remaining:
            raise ValidationError(
                "refusing to remove the last recipient", hint="add another admin first"
            )
        self._write(self.master_key(), remaining)

    def _write(self, secret: str, recipients: list[Recipient]) -> None:
        ciphertext = age.encrypt(self.runner, (secret + "\n").encode(), [r.key for r in recipients])
        # master.age first: if we stop in between, recipients.txt is merely stale
        # and re-running the same command fixes it.
        atomic_write(self.repo.master_age, ciphertext)
        atomic_write(self.repo.recipients_file, recipients_file.render(recipients).encode())
