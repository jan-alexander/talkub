"""The git repo Argo CD pulls from: URL, repoURL rewriting, credentials, git init."""

from __future__ import annotations

import base64
import hashlib
import re
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.error import YAMLError
from ruamel.yaml.scalarstring import LiteralScalarString

from talkub.argoapps import SECRET_TYPE_LABEL
from talkub.config import GitSpec, Repo
from talkub.context import Context
from talkub.core import yamlio
from talkub.core.ephemeral import decrypted, private_tempdir
from talkub.core.errors import CommandFailed, ValidationError
from talkub.core.fsutil import atomic_write
from talkub.core.runner import Runner
from talkub.kube import KubeSession

ARGOCD_KNOWN_HOSTS = frozenset(
    {
        "github.com",
        "gitlab.com",
        "bitbucket.org",
        "ssh.dev.azure.com",
        "vs-ssh.visualstudio.com",
    }
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
        if parts.password or (parts.scheme == "https" and parts.username):
            raise ValidationError(
                "the git URL must not contain credentials",
                hint="use a plain https:// URL and store the token with `talkub repo credentials`",
            )
        if (
            not parts.hostname
            or parts.path in ("", "/")
            or parts.query
            or parts.fragment
        ):
            raise ValidationError("invalid git URL", hint=URL_HINT)
        kind = "ssh" if parts.scheme == "ssh" else "https"
        if kind == "ssh" and port == 22:
            port = None
        return GitUrl(url, kind, parts.hostname, port)
    match = _SCP.fullmatch(url)
    if match is not None:
        return GitUrl(url, "ssh", match["host"].lower(), None)
    raise ValidationError("unsupported git URL (ssh or https only)", hint=URL_HINT)


def git_init(root: Path, url: str, runner: Runner) -> list[str]:
    """`git init -b main` and `origin` = url where missing. Problems become warnings, not errors."""
    if shutil.which("git") is None:
        return [
            f"git not found; run `git init -b main && git remote add origin {url}` in {root}"
        ]
    if not (root / ".git").exists():
        proc = runner.run(["git", "init", "-q", "-b", "main"], cwd=root, check=False)
        if proc.returncode != 0:
            return [
                f"git init failed: {(proc.stderr or b'').decode(errors='replace').strip()}"
            ]
    if (
        runner.run(
            ["git", "remote", "get-url", "origin"], cwd=root, check=False
        ).returncode
        != 0
    ):
        proc = runner.run(
            ["git", "remote", "add", "origin", url], cwd=root, check=False
        )
        if proc.returncode != 0:
            return [
                f"could not add remote origin: {(proc.stderr or b'').decode(errors='replace').strip()}"
            ]
    return []


def origin_hint(root: Path, url: str, runner: Runner) -> str | None:
    """A note when the git `origin` remote does not (yet) point at `url`, or None if it already does."""
    if shutil.which("git") is None:
        return None
    proc = runner.run(["git", "remote", "get-url", "origin"], cwd=root, check=False)
    current = (
        proc.stdout.decode(errors="replace").strip() if proc.returncode == 0 else None
    )
    if current == url:
        return None
    if current is None:
        return f"note: git remote origin is not set; update it with git remote add origin {url}"
    return f"note: git remote origin is {current}; update it with git remote set-url origin {url}"


def _manifest_files(repo: Repo) -> list[Path]:
    files = [repo.gitops_dir / "root.yaml"]
    apps = repo.gitops_dir / "apps"
    if apps.is_dir():
        files += sorted(
            p for p in apps.iterdir() if p.suffix in (".yaml", ".yml") and p.is_file()
        )
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
            raise ValidationError(
                f"{repo.rel(path)} is not valid YAML",
                hint="fix it, then run set-url again",
            ) from None
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


CREDENTIALS_FILE = "repo-credentials.sops.yaml"
KEYSCAN_TIMEOUT = 30.0


def credentials_path(repo: Repo) -> Path:
    return repo.root / "bootstrap" / CREDENTIALS_FILE


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def build_secret(
    cluster: str,
    url: str,
    *,
    ssh_private_key: str | None = None,
    username: str | None = None,
    password: str | None = None,
) -> bytes:
    """The Argo CD repository Secret, in memory. `data` (not stringData): it is applied server-side."""
    data = {"type": _b64("git"), "url": _b64(url)}
    if ssh_private_key is not None:
        data["sshPrivateKey"] = _b64(ssh_private_key)
    else:
        for label, value in (("username", username), ("token", password)):
            if not value or "\n" in value or value != value.strip():
                raise ValidationError(f"the {label} must be a non-empty single line")
        data["username"] = _b64(username)
        data["password"] = _b64(password)
    manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": f"repo-{cluster}",
            "namespace": "argocd",
            "labels": {SECRET_TYPE_LABEL: "repository"},
        },
        "data": data,
    }
    return yamlio.dump(manifest).encode()


def credentials_kind(manifest: bytes) -> str:
    data = yamlio.load(manifest.decode()).get("data") or {}
    return "ssh" if "sshPrivateKey" in data else "https"


def replace_secret_url(manifest: bytes, url: str) -> bytes:
    doc = yamlio.load(manifest.decode())
    doc["data"]["url"] = _b64(url)
    return yamlio.dump(doc).encode()


def write_credentials(ctx: Context, manifest: bytes) -> Path:
    path = credentials_path(ctx.repo)
    atomic_write(
        path,
        ctx.sops.encrypt(
            manifest, filename=path.name, public_key=ctx.keyring.public_key()
        ),
    )
    return path


def generate_ssh_key(runner: Runner, comment: str) -> tuple[str, str]:
    """A fresh ed25519 key pair; the private half only exists in a private temp dir and in memory."""
    with private_tempdir() as work:
        key = work / "key"
        runner.run(
            [
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                comment,
                "-f",
                str(key),
            ]
        )
        return key.read_text(), (work / "key.pub").read_text().strip()


def needs_known_hosts(url: GitUrl) -> bool:
    return url.kind == "ssh" and (
        url.host not in ARGOCD_KNOWN_HOSTS or url.port is not None
    )


def scan_host_keys(runner: Runner, url: GitUrl) -> list[str]:
    argv = ["ssh-keyscan", "-T", "10"]
    if url.port is not None:
        argv += ["-p", str(url.port)]
    argv.append(url.host)
    proc = runner.run(argv, check=False, timeout=KEYSCAN_TIMEOUT)
    lines = [
        line.strip()
        for line in proc.stdout.decode(errors="replace").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    if not lines:
        where = f"{url.host}:{url.port}" if url.port else url.host
        raise CommandFailed(
            f"no SSH host keys from {where}",
            hint="host unreachable or no SSH on that port",
        )
    return lines


def fingerprint(line: str) -> str:
    """'<keytype> SHA256:<base64>' as printed by `ssh-keygen -l`."""
    _, keytype, blob = line.split()[:3]
    digest = hashlib.sha256(base64.b64decode(blob)).digest()
    return f"{keytype} SHA256:{base64.b64encode(digest).decode().rstrip('=')}"


def _submap(data: CommentedMap, key: str) -> CommentedMap:
    """data[key] as a mapping, replacing it with a fresh one if absent or null (e.g. `key:` with no value)."""
    value = data.get(key)
    if not isinstance(value, Mapping):
        value = CommentedMap()
        data[key] = value
    return value


def add_extra_hosts(repo: Repo, lines: list[str]) -> bool:
    """Append known-hosts lines to configs.ssh.extraHosts in gitops/values/argocd.yaml."""
    path = repo.gitops_dir / "values" / "argocd.yaml"
    data = (yamlio.load(path.read_text()) if path.is_file() else None) or CommentedMap()
    ssh = _submap(_submap(data, "configs"), "ssh")
    existing = [
        line for line in str(ssh.get("extraHosts") or "").splitlines() if line.strip()
    ]
    new = [line for line in lines if line not in existing]
    if not new:
        return False
    ssh["extraHosts"] = LiteralScalarString("\n".join(existing + new) + "\n")
    atomic_write(path, yamlio.dump(data).encode())
    return True


def has_extra_hosts(repo: Repo) -> bool:
    """True if gitops/values/argocd.yaml sets a non-empty configs.ssh.extraHosts."""
    path = repo.gitops_dir / "values" / "argocd.yaml"
    if not path.is_file():
        return False
    try:
        data = yamlio.load(path.read_text())
    except YAMLError:
        return False
    if not isinstance(data, Mapping):
        return False
    configs = data.get("configs")
    if not isinstance(configs, Mapping):
        return False
    ssh = configs.get("ssh")
    if not isinstance(ssh, Mapping):
        return False
    extra = ssh.get("extraHosts")
    return isinstance(extra, str) and bool(extra.strip())


@dataclass(frozen=True)
class SshCredentials:
    path: Path
    public_key: str
    known_hosts_written: bool


def create_ssh_credentials(
    ctx: Context, name: str, url: GitUrl, confirm: Callable[[str], bool]
) -> SshCredentials:
    """Host keys are confirmed first; nothing is generated or written if that fails."""
    host_lines: list[str] = []
    if needs_known_hosts(url):
        host_lines = scan_host_keys(ctx.runner, url)
        shown = "\n".join(f"  {fingerprint(line)}" for line in host_lines)
        if not confirm(
            f"SSH host keys of {url.host}:\n{shown}\nTrust them for Argo CD?"
        ):
            raise ValidationError("host keys not confirmed; nothing was written")
    private, public = generate_ssh_key(ctx.runner, f"talkub-{name}")
    written = add_extra_hosts(ctx.repo, host_lines) if host_lines else False
    path = write_credentials(ctx, build_secret(name, url.url, ssh_private_key=private))
    return SshCredentials(path, public, written)


def create_https_credentials(
    ctx: Context, name: str, url: GitUrl, username: str, token: str
) -> Path:
    return write_credentials(
        ctx, build_secret(name, url.url, username=username, password=token)
    )


@dataclass(frozen=True)
class SetUrlResult:
    changed: list[Path]
    credentials_updated: bool
    unchanged: bool
    credentials_need_refresh: bool = False


def set_url(ctx: Context, url: str) -> SetUrlResult:
    """Point settings, gitops repoURLs and stored credentials at `url`. Never touches the cluster."""
    new = classify_url(url)
    settings = ctx.repo.load_settings()
    old = (
        settings.git.url if settings.git is not None else placeholder_url(settings.name)
    )
    if old == new.url:
        return SetUrlResult([], False, True)

    creds = credentials_path(ctx.repo)
    manifest = ctx.sops.decrypt(creds) if creds.is_file() else None
    if manifest is not None and credentials_kind(manifest) != new.kind:
        raise ValidationError(
            f"the stored repo credentials are for {credentials_kind(manifest)} URLs, the new URL is {new.kind}",
            hint="talkub repo credentials --remove, then set-url, then talkub repo credentials",
        )

    credentials_need_refresh = False
    if manifest is not None:
        try:
            old_url = classify_url(old)
        except ValidationError:
            credentials_need_refresh = True
        else:
            credentials_need_refresh = (old_url.host, old_url.port) != (
                new.host,
                new.port,
            )

    changed = rewrite_repo_urls(ctx.repo, old, new.url)
    if not changed and settings.git is None:
        raise ValidationError(
            "no repoURL under gitops/ matches the placeholder URL",
            hint="set the repoURLs by hand, then add `git: {url: ...}` to .talkub.yaml",
        )
    if manifest is not None:
        write_credentials(ctx, replace_secret_url(manifest, new.url))
    settings.git = GitSpec(url=new.url)
    ctx.repo.write_settings(settings)
    return SetUrlResult(changed, manifest is not None, False, credentials_need_refresh)


def apply(ctx: Context, kube: KubeSession) -> None:
    """Repo credentials first (server-side: no last-applied annotation with the key), then the root app."""
    creds = credentials_path(ctx.repo)
    if creds.is_file():
        with decrypted(ctx.sops, {"repo-credentials.yaml": creds}) as plain:
            kube.kubectl(
                ["apply", "--server-side", "-f", str(plain["repo-credentials.yaml"])]
            )
    kube.kubectl(["apply", "-f", str(ctx.repo.gitops_dir / "root.yaml")])


def placeholder_warning(repo: Repo) -> str | None:
    root = repo.gitops_dir / "root.yaml"
    if root.is_file() and "example.invalid/CHANGE-ME" in root.read_text():
        return "gitops/root.yaml still points at the placeholder URL; run `talkub repo set-url <url>`"
    return None
