"""Thin wrapper around the age and age-keygen binaries."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from talkub.core.errors import CommandFailed, DecryptFailed, ValidationError
from talkub.core.runner import Runner


@dataclass(frozen=True)
class KeyPair:
    secret: str
    public: str


def generate(runner: Runner) -> KeyPair:
    output = runner.run(["age-keygen"]).stdout.decode()
    secret = next(
        line for line in output.splitlines() if line.startswith("AGE-SECRET-KEY-")
    )
    return KeyPair(secret=secret, public=public_key(runner, secret))


def public_key(runner: Runner, secret: str) -> str:
    return (
        runner.run(["age-keygen", "-y"], input=(secret + "\n").encode())
        .stdout.decode()
        .strip()
    )


def encrypt(runner: Runner, plaintext: bytes, recipients: Sequence[str]) -> bytes:
    if not recipients:
        raise ValidationError("cannot encrypt without recipients")
    argv = ["age", "--encrypt", "--armor"]
    for recipient in recipients:
        argv += ["-r", recipient]
    return runner.run(argv, input=plaintext).stdout


def decrypt(runner: Runner, path: Path, identities: Sequence[Path]) -> bytes:
    argv = ["age", "--decrypt"]
    for identity in identities:
        argv += ["-i", str(identity)]
    argv.append(str(path))
    try:
        # stderr stays on the terminal: plugins print "touch your YubiKey" / PIN prompts there.
        return runner.run(argv, passthrough_stderr=True).stdout
    except CommandFailed as err:
        raise DecryptFailed(
            f"could not decrypt {path.name}",
            hint="is your hardware key connected, and is your identity listed in keys/recipients.txt?",
        ) from err
