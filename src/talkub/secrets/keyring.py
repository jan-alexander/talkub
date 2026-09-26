"""The cluster master key: keys/master.age, keys/master.pub and keys/recipients.txt.

All SOPS files are encrypted to the master key only. The master key itself is
encrypted to every admin recipient, so adding or removing an admin rewrites a
single small file instead of every SOPS file.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence

from talkub.config import Repo
from talkub.core.errors import NoIdentity, ValidationError
from talkub.core.fsutil import atomic_write
from talkub.core.runner import Runner
from talkub.secrets import age, identity
from talkub.secrets import recipients as recipients_file
from talkub.secrets.recipients import Recipient
from talkub.secrets.sops import Sops, render_sops_config


class Keyring:
    def __init__(
        self, repo: Repo, runner: Runner, env: Mapping[str, str] | None = None
    ) -> None:
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
        path = self.repo.master_pub
        if not path.is_file():
            raise ValidationError(f"{self.repo.rel(path)} is missing")
        return path.read_text().strip()

    def master_key(self) -> str:
        if self._master is None:
            self._master = self._resolve()
        return self._master

    def _resolve(self) -> str:
        if candidates := identity.master_key_candidates_from_env(self.env):
            # Explicit env wins, but only if it really holds this cluster's key:
            # SOPS_AGE_KEY_FILE often points at an unrelated personal key file.
            expected = self.public_key()
            for candidate in candidates:
                if age.public_key(self.runner, candidate) == expected:
                    return candidate
            raise NoIdentity(
                "SOPS_AGE_KEY/SOPS_AGE_KEY_FILE is set but does not contain this cluster's master key",
                hint="unset it for talkub, or check keys/master.pub",
            )
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
            raise ValidationError(
                f"{self.repo.rel(self.repo.master_age)} already exists"
            )
        if not recipients:
            raise ValidationError("at least one recipient is required")
        pair = age.generate(self.runner)
        # master.pub first: _write checks the secret against it.
        atomic_write(self.repo.master_pub, (pair.public + "\n").encode())
        self._write(pair.secret, list(recipients))
        self._master = pair.secret
        return pair.secret

    def add(self, recipient: Recipient) -> None:
        current = self.recipients()
        for existing in current:
            if existing.name == recipient.name:
                raise ValidationError(f"recipient '{recipient.name}' already exists")
            if existing.key == recipient.key:
                raise ValidationError(
                    f"this key is already registered as '{existing.name}'"
                )
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
        # Invariant: master.age always holds the key matching master.pub, which every
        # SOPS file is encrypted to. Writing any other key would lock everyone out.
        if age.public_key(self.runner, secret) != self.public_key():
            raise ValidationError(
                "refusing to write keys/master.age: the key does not match keys/master.pub"
            )
        ciphertext = age.encrypt(
            self.runner, (secret + "\n").encode(), [r.key for r in recipients]
        )
        # master.age first: if we stop in between, recipients.txt is merely stale
        # and re-running the same command fixes it.
        atomic_write(self.repo.master_age, ciphertext)
        atomic_write(
            self.repo.recipients_file, recipients_file.render(recipients).encode()
        )

    def rotate(self, sops: Sops) -> None:
        """Replace the master key and re-encrypt every SOPS file.

        The new key is written to master.next.age before any SOPS file changes, so
        an interruption never loses the key those files are encrypted to.
        """
        pending = self.repo.master_next_age
        if pending.exists():
            raise ValidationError(
                "a previous key rotation was interrupted",
                hint=(
                    "keys/master.next.age holds the newer key; decrypt it with `age -d`, "
                    "check which key the SOPS files use, finish manually and delete master.next.age"
                ),
            )
        recipients = self.recipients()
        plaintexts = {path: sops.decrypt(path) for path in self.repo.sops_files()}
        new = age.generate(self.runner)

        atomic_write(
            pending,
            age.encrypt(
                self.runner, (new.secret + "\n").encode(), [r.key for r in recipients]
            ),
        )
        for path, plaintext in plaintexts.items():
            atomic_write(
                path, sops.encrypt(plaintext, filename=path.name, public_key=new.public)
            )
        atomic_write(self.repo.master_pub, (new.public + "\n").encode())
        atomic_write(self.repo.sops_config, render_sops_config(new.public).encode())
        os.replace(pending, self.repo.master_age)
        self._master = new.secret
