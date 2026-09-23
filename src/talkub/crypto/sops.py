"""Thin wrapper around the sops binary.

The master key is handed to sops via SOPS_AGE_KEY in the child environment only.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Callable
from pathlib import Path

from talkub.ephemeral import private_file
from talkub.errors import CommandFailed, DecryptFailed
from talkub.fsutil import atomic_write
from talkub.runner import Runner


def render_sops_config(public_key: str) -> str:
    return (
        "# Managed by talkub. Every *.sops.yaml is encrypted to the cluster master key.\n"
        "creation_rules:\n"
        "  - path_regex: \\.sops\\.yaml$\n"
        f"    age: {public_key}\n"
    )


class Sops:
    def __init__(self, runner: Runner, master_key: Callable[[], str]) -> None:
        self.runner = runner
        self._master_key = master_key

    def _key_env(self) -> dict[str, str]:
        return {"SOPS_AGE_KEY": self._master_key()}

    def decrypt(self, path: Path) -> bytes:
        try:
            return self.runner.run(["sops", "decrypt", str(path)], env=self._key_env()).stdout
        except CommandFailed as err:
            raise DecryptFailed(f"sops could not decrypt {path}", hint=err.hint) from err

    def encrypt(self, plaintext: bytes, *, filename: str, public_key: str) -> bytes:
        argv = [
            "sops", "encrypt",
            "--age", public_key,
            "--input-type", "yaml",
            "--output-type", "yaml",
            "--filename-override", filename,
            "/dev/stdin",
        ]
        return self.runner.run(argv, input=plaintext).stdout

    def edit(self, path: Path, public_key: str) -> int:
        """Edit `path` in $SOPS_EDITOR/$EDITOR; a missing file starts out empty.

        Not `sops edit`: sops would start the editor with SOPS_AGE_KEY in its
        environment. Here the editor runs via Runner.call, which strips key material.
        Returns the editor's exit code; nothing is written unless it succeeded and
        the content changed.
        """
        original = self.decrypt(path) if path.exists() else b""
        editor = shlex.split(os.environ.get("SOPS_EDITOR") or os.environ.get("EDITOR") or "vi")
        with private_file(path.name, original) as plaintext:
            code = self.runner.call([*editor, str(plaintext)])
            if code != 0:
                return code
            edited = plaintext.read_bytes()
            if edited == original:
                return 0
            ciphertext = self.encrypt(edited, filename=path.name, public_key=public_key)
        atomic_write(path, ciphertext)
        return 0
