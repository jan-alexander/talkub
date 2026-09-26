"""`Finding`: a single lint/check result, shared by `talkub.quality.lint` and
`talkub.argocd.app_files`.

It lives in `core` because `app_files` sits below `quality` and must not import `lint`.
`talkub.quality.lint` re-exports `Finding` so existing imports of it from there keep working.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Finding:
    level: str  # "error" | "warning"
    path: str  # repo-relative, or an app name prefix like "app coredns"
    message: str
    line: int | None = None
