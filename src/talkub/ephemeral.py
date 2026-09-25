"""Short-lived plaintext: private temp dirs that are always removed."""

from __future__ import annotations

import os
import shutil
import signal
import tempfile
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

# SIGHUP covers a closed terminal window.
_TERMINATION_SIGNALS = (signal.SIGTERM, signal.SIGHUP)


class Decryptor(Protocol):
    def decrypt(self, path: Path) -> bytes: ...


@contextmanager
def private_tempdir() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="talkub-"))  # mkdtemp creates it with mode 0700
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@contextmanager
def _termination_as_exit() -> Iterator[None]:
    """Turn SIGTERM/SIGHUP into SystemExit so `finally` blocks run and clean up."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def handler(signum, frame):
        raise SystemExit(128 + signum)

    previous = {sig: signal.signal(sig, handler) for sig in _TERMINATION_SIGNALS}
    try:
        yield
    finally:
        for sig, old in previous.items():
            signal.signal(sig, old)


def write_private(target: Path, data: bytes) -> None:
    """Create `target` (must not exist) readable by the owner only."""
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


@contextmanager
def decrypted(sops: Decryptor, files: Mapping[str, Path]) -> Iterator[dict[str, Path]]:
    """Decrypt `files` (name -> encrypted path) into a private temp dir for the block's duration."""
    with _termination_as_exit(), private_tempdir() as directory:
        paths: dict[str, Path] = {}
        for name, source in files.items():
            target = directory / name
            write_private(target, sops.decrypt(source))
            paths[name] = target
        yield paths


@contextmanager
def private_file(name: str, content: bytes) -> Iterator[Path]:
    """A 0600 file holding `content` in a private temp dir for the block's duration."""
    with _termination_as_exit(), private_tempdir() as directory:
        target = directory / name
        write_private(target, content)
        yield target
