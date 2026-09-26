"""`Finding`: a single lint/check result, shared by `talkub.lint` and `talkub.argoapps`.

Split out from `talkub.lint` so `talkub.argoapps` can use it without an import cycle (lint imports
`HelmSource`/`helm_sources` from argoapps). `talkub.lint` re-exports `Finding` so existing imports
of it from there keep working.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Finding:
    level: str  # "error" | "warning"
    path: str  # repo-relative, or an app name prefix like "app coredns"
    message: str
    line: int | None = None
