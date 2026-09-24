"""Reading secrets from a file or a hidden prompt — never from the command line."""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

from talkub.errors import ValidationError


def read_secret(path: Path | None, prompt: str, repo_root: Path | None = None) -> str:
    if path is None:
        return getpass.getpass(prompt).strip()
    try:
        value = path.expanduser().read_text().strip()
    except OSError as err:
        raise ValidationError(f"cannot read {path}: {err.strerror}") from None
    if repo_root is not None and path.expanduser().resolve().is_relative_to(repo_root.resolve()):
        print(
            f"warning: {path} is inside the cluster repo; the plaintext secret file must not be "
            "committed — delete it after use",
            file=sys.stderr,
        )
    return value
