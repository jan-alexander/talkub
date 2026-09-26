"""Personal age identities and master key lookup from the environment."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

from talkub.core.errors import CommandFailed, NoIdentity, ValidationError
from talkub.core.fsutil import atomic_write
from talkub.core.runner import Runner
from talkub.secrets.recipients import Recipient

KINDS = ("software", "yubikey", "se")

_SECRET = re.compile(r"^AGE-SECRET-KEY-1[0-9A-Z]+$", re.MULTILINE)
_RECIPIENT = re.compile(r"(?:public key|recipient):\s*(age1[0-9a-z]+)", re.IGNORECASE)


def identities_dir(env: Mapping[str, str]) -> Path:
    base = env.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "talkub" / "identities"


def _all_secrets(text: str, source: str) -> list[str]:
    secrets = _SECRET.findall(text)
    if not secrets:
        raise NoIdentity(f"{source} does not contain an AGE-SECRET-KEY")
    return secrets


def master_key_candidates_from_env(env: Mapping[str, str]) -> list[str]:
    """Stage 1 of the resolution order: the master key itself (CI/automation).

    Returns every age secret key found; the caller picks the one matching
    keys/master.pub. Empty when neither variable is set.
    """
    if value := env.get("SOPS_AGE_KEY", "").strip():
        return _all_secrets(value, "SOPS_AGE_KEY")
    if path := env.get("SOPS_AGE_KEY_FILE"):
        try:
            return _all_secrets(
                Path(path).expanduser().read_text(), "SOPS_AGE_KEY_FILE"
            )
        except OSError as err:
            raise NoIdentity(
                f"cannot read SOPS_AGE_KEY_FILE {path}: {err.strerror}"
            ) from err
    return []


def identity_files(env: Mapping[str, str]) -> list[Path]:
    """Stages 2 and 3: TALKUB_IDENTITY, else every file in the identities dir."""
    if raw := env.get("TALKUB_IDENTITY"):
        files = [Path(part).expanduser() for part in raw.split(os.pathsep) if part]
        missing = [str(f) for f in files if not f.is_file()]
        if missing:
            raise NoIdentity(
                f"TALKUB_IDENTITY points to missing file(s): {', '.join(missing)}"
            )
        return files
    directory = identities_dir(env)
    if not directory.is_dir():
        return []
    return sorted(
        p for p in directory.iterdir() if p.is_file() and not p.name.startswith(".")
    )


def recipient_of(path: Path) -> str | None:
    """Read the public key from the comment header of an identity file."""
    for line in path.read_text().splitlines():
        if line.lstrip().startswith("#") and (match := _RECIPIENT.search(line)):
            return match.group(1)
    return None


def create_identity(
    runner: Runner, kind: str, directory: Path, name: str | None = None
) -> tuple[Path, str]:
    if kind not in KINDS:
        raise ValidationError(
            f"unknown identity kind {kind!r}", hint=f"use one of: {', '.join(KINDS)}"
        )
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    path = directory / f"{name or kind}.txt"
    if path.exists():
        raise ValidationError(
            f"identity {path} already exists", hint="choose another --name"
        )

    if kind == "software":
        runner.run(["age-keygen", "-o", str(path)])
    elif kind == "yubikey":
        # Interactive: the plugin asks for slot, PIN and touch policy on the terminal.
        if runner.call(["age-plugin-yubikey", "--generate"]) != 0:
            raise CommandFailed("age-plugin-yubikey --generate failed")
        atomic_write(
            path, runner.run(["age-plugin-yubikey", "--identity"]).stdout, mode=0o600
        )
    else:
        runner.run(
            [
                "age-plugin-se",
                "keygen",
                "--access-control=any-biometry",
                "-o",
                str(path),
            ]
        )

    os.chmod(path, 0o600)
    public = recipient_of(path)
    if public is None:
        raise ValidationError(f"could not read the public key from {path}")
    return path, public


def own_recipients(env: Mapping[str, str], user: str) -> list[Recipient]:
    """Recipients for the caller's own identities, named '<user>-<file stem>'."""
    files = identity_files(env)
    if not files:
        raise NoIdentity(
            "no age identity found",
            hint=f"run `talkub identity new` (stored in {identities_dir(env)}) or set TALKUB_IDENTITY",
        )
    safe_user = re.sub(r"[^A-Za-z0-9._-]", "-", user)
    result = []
    for path in files:
        key = recipient_of(path)
        if key is None:
            raise ValidationError(
                f"cannot determine the public key of {path}",
                hint="the file needs a '# public key: age1...' comment",
            )
        result.append(Recipient(f"{safe_user}-{path.stem}", key))
    return result
