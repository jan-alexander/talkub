"""The git repo Argo CD pulls from: URL, repoURL rewriting, credentials, git init."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from talkub.errors import ValidationError

ARGOCD_KNOWN_HOSTS = frozenset(
    {"github.com", "gitlab.com", "bitbucket.org", "ssh.dev.azure.com", "vs-ssh.visualstudio.com"}
)
URL_HINT = "e.g. git@github.com:me/cluster.git or https://github.com/me/cluster.git"
_SCP = re.compile(r"(?P<user>[^@/:\s]+)@(?P<host>[A-Za-z0-9.-]+):(?P<path>\S+)")


@dataclass(frozen=True)
class GitUrl:
    url: str
    kind: str  # "ssh" or "https"
    host: str
    port: int | None


def placeholder_url(name: str) -> str:
    return f"https://example.invalid/CHANGE-ME/{name}.git"


def classify_url(url: str) -> GitUrl:
    """SSH (scp form or ssh://) or HTTPS; errors never echo the URL (it may hold a token)."""
    url = url.strip()
    if url.startswith(("ssh://", "https://")):
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            raise ValidationError("invalid git URL", hint=URL_HINT) from None
        if parts.scheme == "https" and (parts.username or parts.password):
            raise ValidationError(
                "the git URL must not contain credentials",
                hint="use a plain https:// URL and store the token with `talkub repo credentials`",
            )
        if not parts.hostname or parts.path in ("", "/") or parts.query or parts.fragment:
            raise ValidationError("invalid git URL", hint=URL_HINT)
        kind = "ssh" if parts.scheme == "ssh" else "https"
        if kind == "ssh" and port == 22:
            port = None
        return GitUrl(url, kind, parts.hostname, port)
    match = _SCP.fullmatch(url)
    if match is not None:
        return GitUrl(url, "ssh", match["host"].lower(), None)
    raise ValidationError("unsupported git URL (ssh or https only)", hint=URL_HINT)
