"""The git repo Argo CD pulls from: URL, repoURL rewriting, credentials, git init."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from ruamel.yaml.error import YAMLError

from talkub import yamlio
from talkub.errors import ValidationError
from talkub.fsutil import atomic_write
from talkub.repo import Repo

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


def _manifest_files(repo: Repo) -> list[Path]:
    files = [repo.gitops_dir / "root.yaml"]
    apps = repo.gitops_dir / "apps"
    if apps.is_dir():
        files += sorted(p for p in apps.iterdir() if p.suffix in (".yaml", ".yml") and p.is_file())
    return [p for p in files if p.is_file()]


def _sources(doc: object) -> list[Mapping]:
    """spec.source and every spec.sources[] entry of an Application-like document."""
    spec = doc.get("spec") if isinstance(doc, Mapping) else None
    if not isinstance(spec, Mapping):
        return []
    found = [spec["source"]] if isinstance(spec.get("source"), Mapping) else []
    if isinstance(spec.get("sources"), list):
        found += [s for s in spec["sources"] if isinstance(s, Mapping)]
    return found


def rewrite_repo_urls(repo: Repo, old: str, new: str) -> list[Path]:
    """Point every repoURL equal to `old` at `new`. Parses everything before writing anything."""
    pending: list[tuple[Path, list]] = []
    for path in _manifest_files(repo):
        try:
            docs = yamlio.load_all(path.read_text())
        except YAMLError:
            raise ValidationError(f"{repo.rel(path)} is not valid YAML", hint="fix it, then run set-url again") from None
        hits = 0
        for doc in docs:
            for source in _sources(doc):
                if source.get("repoURL") == old:
                    source["repoURL"] = new
                    hits += 1
        if hits:
            pending.append((path, docs))
    for path, docs in pending:
        atomic_write(path, yamlio.dump_all(docs).encode())
    return [path for path, _ in pending]
