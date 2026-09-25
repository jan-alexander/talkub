"""Entry point for the detached clipboard-clearing helper (`python -m talkub.clipboard_clear`).

Started via `talkub.clipboard.schedule_clear` / `Runner.spawn_detached`. Reads a SHA-256 hex
digest from stdin (never the plaintext), sleeps `TALKUB_CLIPBOARD_CLEAR_SECONDS` (default 45),
then clears the clipboard only if its current content still hashes to that digest. Never prints
anything, so nothing leaks even if the stdout/stderr redirection by the parent were absent.
"""

from __future__ import annotations

import hashlib
import os
import sys
import time

from talkub import clipboard
from talkub.runner import Runner


def main() -> None:
    digest = sys.stdin.read().strip()
    time.sleep(float(os.environ.get("TALKUB_CLIPBOARD_CLEAR_SECONDS", "45")))
    found = clipboard.tool()
    if found is None:
        return
    current = Runner().run(found.paste, check=False).stdout
    if hashlib.sha256(current).hexdigest() == digest:
        Runner().feed(found.clear, input=b"")


if __name__ == "__main__":
    main()
