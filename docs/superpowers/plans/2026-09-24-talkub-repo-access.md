# talkub Git Repo Access Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Store the git URL in `.talkub.yaml`, change it with `talkub repo set-url`, give Argo CD SSH/HTTPS credentials for private repos (`talkub repo credentials`), apply them with `talkub repo apply` (also used by `bootstrap`), and let `init` run `git init` + `git remote add origin`.

**Architecture:** A new core module `src/talkub/gitrepo.py` holds URL classification, the round-trip rewrite of `repoURL`s under `gitops/`, the repository Secret (SOPS-encrypted in `bootstrap/repo-credentials.sops.yaml`), SSH key generation, known-hosts handling, `set_url`, `apply` and `git_init`. `src/talkub/cli/repo.py` is a thin Typer group. `bootstrap` calls `gitrepo.apply` instead of applying `root.yaml` itself.

**Tech Stack:** Python ≥ 3.12, Typer, ruamel.yaml, pytest; age, sops, kubectl, ssh-keygen, ssh-keyscan, git.

**Spec:** `docs/superpowers/specs/2026-09-24-talkub-repo-access-design.md` (earlier specs still bind).

## Global Constraints

- All code, identifiers, comments, docstrings and CLI output in **English** (chat with the user is German, code is not).
- `runner.py` is the only module that starts subprocesses; argv lists only. `Runner.run(..., timeout=...)` exists (exit code 124 on timeout with `check=False`).
- Credentials never touch disk in plaintext outside `private_tempdir()`/`decrypted()`/`private_file()` (0700 dirs, 0600 files). They are never on a command line and never printed. The only file written is `bootstrap/repo-credentials.sops.yaml`, encrypted with `ctx.sops.encrypt(..., filename=path.name, public_key=ctx.keyring.public_key())` and written with `atomic_write`.
- **Ruling (plan vs. spec §5):** the repository Secret uses `data` (base64) instead of `stringData`. Reason: the Secret is applied with `kubectl apply --server-side` (no `last-applied-configuration` annotation holding the key, same as the sealing-key restore), and `data` avoids any server-side-apply field-ownership quirks of the write-only `stringData`. Everything else in spec §5 stands.
- Secret shape: `apiVersion: v1`, `kind: Secret`, `metadata.name: repo-<cluster name>`, `metadata.namespace: argocd`, label `argocd.argoproj.io/secret-type: repository`, `data.type = b64("git")`, `data.url = b64(url)`, plus either `data.sshPrivateKey` or `data.username` + `data.password`.
- Placeholder URL: `https://example.invalid/CHANGE-ME/<name>.git`.
- Argo CD default known hosts: `github.com`, `gitlab.com`, `bitbucket.org`, `ssh.dev.azure.com`, `vs-ssh.visualstudio.com`.
- Error messages about git URLs never echo the URL (it might contain a token).
- Commits end with `Co-Authored-By: <the model that wrote the commit> <noreply@anthropic.com>`.
- Run tests with `uv run pytest`; tests needing a binary use `need(...)` from `tests/helpers.py` and skip when it is missing.

## Review Focus

1. **Token in an HTTPS URL** (`https://user:tok@host/x.git`) must be rejected by `classify_url`, and the error must not contain the token (test in Task 1).
2. **Kind mismatch on `set-url`** (SSH credentials exist, new URL is HTTPS) must fail before any file changes (test in Task 4).
3. **Invalid YAML in one app file** must abort `rewrite_repo_urls` before any file is written (test in Task 2).
4. **Unreachable SSH host** (`ssh-keyscan` returns nothing) must fail before a key is generated or any file is written (test in Task 3).
5. **Private key leakage:** after `create_ssh_credentials`, no file in the repo contains `PRIVATE KEY`, and no recorded argv contains key material (test in Task 3).
6. **Re-running credentials** for the same custom host must not duplicate `configs.ssh.extraHosts` lines (test in Task 3).

---

### Task 1: `GitSpec` in settings, `classify_url`, placeholder

**Files:**
- Modify: `src/talkub/repo.py` (new `GitSpec`, `Settings.git`, `to_dict`, `from_dict`)
- Create: `src/talkub/gitrepo.py`
- Test: `tests/test_gitrepo.py` (new), `tests/test_repo.py`

**Interfaces:**
- Produces: `talkub.repo.GitSpec(url: str)` (frozen dataclass); `Settings.git: GitSpec | None = None` (last field).
- Produces: `talkub.gitrepo.GitUrl(url: str, kind: str, host: str, port: int | None)` (frozen dataclass; `kind` is `"ssh"` or `"https"`; `port` is `None` for the default port, and for SSH port 22 is normalised to `None`).
- Produces: `classify_url(url: str) -> GitUrl` (strips whitespace; raises `ValidationError`); `placeholder_url(name: str) -> str`; constant `ARGOCD_KNOWN_HOSTS: frozenset[str]`.

- [ ] **Step 1: Write the failing tests**

`tests/test_gitrepo.py`:

```python
import pytest

from talkub import gitrepo
from talkub.errors import ValidationError
from talkub.gitrepo import GitUrl, classify_url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "git@github.com:me/cluster.git",
            GitUrl("git@github.com:me/cluster.git", "ssh", "github.com", None),
        ),
        (
            "  git@git.example.com:/srv/cluster.git ",
            GitUrl("git@git.example.com:/srv/cluster.git", "ssh", "git.example.com", None),
        ),
        (
            "ssh://git@git.example.com/me/c.git",
            GitUrl("ssh://git@git.example.com/me/c.git", "ssh", "git.example.com", None),
        ),
        (
            "ssh://git@git.example.com:2222/me/c.git",
            GitUrl(
                "ssh://git@git.example.com:2222/me/c.git",
                "ssh",
                "git.example.com",
                2222,
            ),
        ),
        (
            "ssh://git@git.example.com:22/me/c.git",
            GitUrl("ssh://git@git.example.com:22/me/c.git", "ssh", "git.example.com", None),
        ),
        (
            "https://github.com/me/cluster.git",
            GitUrl("https://github.com/me/cluster.git", "https", "github.com", None),
        ),
        (
            "https://git.example.com:8443/me/c.git",
            GitUrl(
                "https://git.example.com:8443/me/c.git",
                "https",
                "git.example.com",
                8443,
            ),
        ),
    ],
)
def test_classify_url_accepts(url, expected):
    assert classify_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://github.com/me/c.git",
        "file:///srv/c.git",
        "github.com/me/c.git",
        "https://github.com",
        "https://github.com/",
        "ssh://git@host:notaport/x.git",
        "git@:path",
        "https://github.com/me/c.git?x=1",
    ],
)
def test_classify_url_rejects(url):
    with pytest.raises(ValidationError) as exc:
        classify_url(url)
    assert "e.g. git@github.com:" in (exc.value.hint or "")


def test_classify_url_rejects_embedded_credentials_without_echoing_them():
    with pytest.raises(ValidationError) as exc:
        classify_url("https://jam:ghp_SECRET123@github.com/me/c.git")
    assert "ghp_SECRET123" not in str(exc.value)
    assert "ghp_SECRET123" not in (exc.value.hint or "")
    assert "talkub repo credentials" in (exc.value.hint or "")


def test_placeholder_url():
    assert gitrepo.placeholder_url("homelab") == "https://example.invalid/CHANGE-ME/homelab.git"
```

Append to `tests/test_repo.py`:

```python
def test_git_block_round_trips(tmp_path):
    from helpers import sample_settings
    from talkub.repo import GitSpec, Repo

    repo = Repo(tmp_path)
    settings = sample_settings()
    assert settings.git is None
    repo.write_settings(settings)
    assert "git:" not in repo.settings_file.read_text()  # absent block stays absent
    settings.git = GitSpec(url="git@github.com:me/c.git")
    repo.write_settings(settings)
    assert repo.load_settings().git == GitSpec(url="git@github.com:me/c.git")


@pytest.mark.parametrize("raw", ["git: nope\n", "git: {}\n", "git: {url: ''}\n"])
def test_git_block_must_have_url(tmp_path, raw):
    from helpers import sample_settings
    from talkub import yamlio
    from talkub.errors import ValidationError
    from talkub.repo import Repo

    repo = Repo(tmp_path)
    repo.write_settings(sample_settings())
    data = yamlio.load(repo.settings_file.read_text())
    data["git"] = yamlio.load(raw)["git"]
    repo.settings_file.write_text(yamlio.dump(data))
    with pytest.raises(ValidationError):
        repo.load_settings()
```

(If `tests/test_repo.py` does not import `pytest` yet, add `import pytest` at the top.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_gitrepo.py tests/test_repo.py -q`
Expected: FAIL (`ModuleNotFoundError: talkub.gitrepo`, `ImportError: GitSpec`).

- [ ] **Step 3: Implement**

In `src/talkub/repo.py`, next to `TailscaleSpec`:

```python
@dataclass(frozen=True)
class GitSpec:
    url: str
```

Add `git: GitSpec | None = None` as the **last** field of `Settings`. In `to_dict`, after the tailscale block:

```python
        if self.git is not None:
            data["git"] = {"url": self.git.url}
```

In `from_dict`, before `return cls(...)`, and pass `git=git`:

```python
git = None
raw_git = data.get("git")
if raw_git is not None:
    if not isinstance(raw_git, Mapping):
        raise ValidationError(f"{MARKER}: 'git' must be a mapping")
    url = _optional_text(raw_git.get("url"), "git.url")
    if url is None:
        raise ValidationError(f"{MARKER}: 'git' needs a url", hint="talkub repo set-url <url>")
    git = GitSpec(url=url)
```

(`repo.py` must not import `gitrepo`: `gitrepo` imports `repo`. URL syntax is checked by `gitrepo.classify_url` at the places that act on it.)

Create `src/talkub/gitrepo.py`:

```python
"""The git repo Argo CD pulls from: URL, repoURL rewriting, credentials, git init."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from talkub.errors import ValidationError

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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_gitrepo.py tests/test_repo.py -q`
Expected: PASS. Then `uv run pytest -q` — all green (the new `Settings` field has a default).

- [ ] **Step 5: Commit**

```bash
git add src/talkub/repo.py src/talkub/gitrepo.py tests/test_gitrepo.py tests/test_repo.py
git commit -m "feat: git URL in settings and git URL classification"
```

---

### Task 2: Rewrite `repoURL`s under `gitops/`

**Files:**
- Modify: `src/talkub/yamlio.py` (add `dump_all`)
- Modify: `src/talkub/gitrepo.py`
- Test: `tests/test_gitrepo.py`

**Interfaces:**
- Consumes: `talkub.repo.Repo` (`gitops_dir`, `rel`), `yamlio.load_all`, `talkub.fsutil.atomic_write`.
- Produces: `yamlio.dump_all(docs: list) -> str`; `gitrepo.rewrite_repo_urls(repo: Repo, old: str, new: str) -> list[Path]` (paths written, in order: `gitops/root.yaml`, then `gitops/apps/*.yaml|*.yml` sorted).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_gitrepo.py`:

```python
from talkub.fsutil import atomic_write
from talkub.repo import Repo
from talkub.scaffold import TEMPLATES

OLD = "https://example.invalid/CHANGE-ME/test.git"
NEW = "git@github.com:me/test.git"


def _gitops(tmp_path) -> Repo:
    """root.yaml + the two app templates rendered with OLD, as `init` writes them."""
    from string import Template

    repo = Repo(tmp_path)
    values = {"git_url": OLD, "name": "test"}
    for rel in (
        "gitops/root.yaml",
        "gitops/apps/argocd.yaml",
        "gitops/apps/sealed-secrets.yaml",
    ):
        template = (TEMPLATES / "repo" / (rel + ".tmpl")).read_text()
        atomic_write(repo.root / rel, Template(template).safe_substitute(values).encode())
    return repo


def test_rewrite_changes_only_matching_repo_urls_and_keeps_comments(tmp_path):
    repo = _gitops(tmp_path)
    before = {p: p.read_text() for p in (repo.gitops_dir).rglob("*.yaml")}
    changed = gitrepo.rewrite_repo_urls(repo, OLD, NEW)
    assert [repo.rel(p) for p in changed] == [
        "gitops/root.yaml",
        "gitops/apps/argocd.yaml",
        "gitops/apps/sealed-secrets.yaml",
    ]
    for path, text in before.items():
        assert path.read_text() == text.replace(OLD, NEW)  # byte-identical apart from the URL
    assert "argoproj.github.io/argo-helm" in (repo.gitops_dir / "apps/argocd.yaml").read_text()


def test_rewrite_handles_multi_document_files_and_skips_untouched_files(tmp_path):
    repo = _gitops(tmp_path)
    other = repo.gitops_dir / "apps" / "extra.yml"
    other.write_text(
        "# two apps\n"
        "apiVersion: argoproj.io/v1alpha1\nkind: Application\nspec:\n  source:\n    repoURL: " + OLD + "\n"
        "---\n"
        "apiVersion: argoproj.io/v1alpha1\nkind: Application\nspec:\n  source:\n    repoURL: https://charts.example.com\n"
    )
    unrelated = repo.gitops_dir / "apps" / "unrelated.yaml"
    unrelated.write_text("kind: ConfigMap\ndata: {a: b}\n")
    mtime = unrelated.stat().st_mtime_ns
    gitrepo.rewrite_repo_urls(repo, OLD, NEW)
    text = other.read_text()
    assert text.startswith("# two apps\n") and text.count(NEW) == 1 and "https://charts.example.com" in text
    assert unrelated.stat().st_mtime_ns == mtime


def test_rewrite_without_matches_writes_nothing(tmp_path):
    repo = _gitops(tmp_path)
    assert gitrepo.rewrite_repo_urls(repo, "https://other.example/x.git", NEW) == []


def test_rewrite_aborts_on_invalid_yaml_before_writing_anything(tmp_path):
    repo = _gitops(tmp_path)
    root_before = (repo.gitops_dir / "root.yaml").read_text()
    (repo.gitops_dir / "apps" / "broken.yaml").write_text("spec: [unclosed\n")
    with pytest.raises(ValidationError) as exc:
        gitrepo.rewrite_repo_urls(repo, OLD, NEW)
    assert "gitops/apps/broken.yaml" in str(exc.value)
    assert (repo.gitops_dir / "root.yaml").read_text() == root_before
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_gitrepo.py -q`
Expected: FAIL (`AttributeError: ... rewrite_repo_urls`).

- [ ] **Step 3: Implement**

`src/talkub/yamlio.py`:

```python
def dump_all(docs: list[Any]) -> str:
    buffer = StringIO()
    _yaml().dump_all(docs, buffer)
    return buffer.getvalue()
```

`src/talkub/gitrepo.py` (add imports `from collections.abc import Mapping`, `from pathlib import Path`, `from ruamel.yaml.error import YAMLError`, `from talkub import yamlio`, `from talkub.fsutil import atomic_write`, `from talkub.repo import Repo`):

```python
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
```

If the byte-identical assertion in the first test fails because `dump_all` adds a leading `---` or changes a single-document file, dump single-document files with `yamlio.dump(docs[0])` and multi-document files with `dump_all`; keep the test as is.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_gitrepo.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/talkub/yamlio.py src/talkub/gitrepo.py tests/test_gitrepo.py
git commit -m "feat: rewrite gitops repoURLs round-trip"
```

---

### Task 3: Repository credentials (Secret, SSH key, known hosts, HTTPS)

**Files:**
- Modify: `src/talkub/gitrepo.py`
- Modify: `src/talkub/runner.py` (`INSTALL_HINTS` for `git`, `ssh-keygen`, `ssh-keyscan`)
- Test: `tests/test_gitrepo_credentials.py` (new)

**Interfaces:**
- Consumes: `GitUrl`, `classify_url`, `ARGOCD_KNOWN_HOSTS` (Task 1); `Context` (`repo`, `runner`, `sops`, `keyring`); `Settings.name`; `private_tempdir`; `Runner.run(..., timeout=)`.
- Produces (all in `talkub.gitrepo`):
  - `CREDENTIALS_FILE = "repo-credentials.sops.yaml"`; `SECRET_TYPE_LABEL = "argocd.argoproj.io/secret-type"`
  - `credentials_path(repo: Repo) -> Path` → `repo.root / "bootstrap" / CREDENTIALS_FILE`
  - `build_secret(cluster: str, url: str, *, ssh_private_key: str | None = None, username: str | None = None, password: str | None = None) -> bytes`
  - `credentials_kind(manifest: bytes) -> str` (`"ssh"` if `data.sshPrivateKey` exists, else `"https"`)
  - `replace_secret_url(manifest: bytes, url: str) -> bytes`
  - `write_credentials(ctx: Context, manifest: bytes) -> Path`
  - `generate_ssh_key(runner: Runner, comment: str) -> tuple[str, str]` (private, public)
  - `needs_known_hosts(url: GitUrl) -> bool`
  - `scan_host_keys(runner: Runner, url: GitUrl) -> list[str]`
  - `fingerprint(line: str) -> str` (e.g. `"ssh-ed25519 SHA256:abc…"`)
  - `add_extra_hosts(repo: Repo, lines: list[str]) -> bool`
  - `@dataclass(frozen=True) SshCredentials(path: Path, public_key: str, known_hosts_written: bool)`
  - `create_ssh_credentials(ctx: Context, name: str, url: GitUrl, confirm: Callable[[str], bool]) -> SshCredentials`
  - `create_https_credentials(ctx: Context, name: str, url: GitUrl, username: str, token: str) -> Path`

- [ ] **Step 1: Write the failing tests**

`tests/test_gitrepo_credentials.py`:

```python
import base64
import subprocess

import pytest

from helpers import need
from talkub import gitrepo, yamlio
from talkub.context import Context
from talkub.errors import CommandFailed, ValidationError
from talkub.gitrepo import classify_url

pytestmark = need("age", "age-keygen", "sops", "ssh-keygen")

VALUES = "# Helm values for Argo CD\ndex:\n  enabled: false\n"


def _host_line(tmp_path, host="[git.example.com]:2222"):
    key = tmp_path / "hostkey"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    kind, blob = key.with_suffix(".pub").read_text().split()[:2]
    return f"{host} {kind} {blob}"


def _secret(ctx):
    return yamlio.load(ctx.sops.decrypt(gitrepo.credentials_path(ctx.repo)).decode())


def _b64(value):
    return base64.b64decode(value).decode()


@pytest.fixture
def ctx(repo_with_configs):
    (repo_with_configs.gitops_dir / "values").mkdir(parents=True, exist_ok=True)
    (repo_with_configs.gitops_dir / "values" / "argocd.yaml").write_text(VALUES)
    return Context.open(repo_with_configs)


def test_build_secret_shape():
    doc = yamlio.load(gitrepo.build_secret("homelab", "https://h/x.git", username="u", password="p").decode())
    assert doc["kind"] == "Secret" and doc["metadata"]["name"] == "repo-homelab"
    assert doc["metadata"]["namespace"] == "argocd"
    assert doc["metadata"]["labels"] == {"argocd.argoproj.io/secret-type": "repository"}
    assert {k: _b64(v) for k, v in doc["data"].items()} == {
        "type": "git",
        "url": "https://h/x.git",
        "username": "u",
        "password": "p",
    }
    assert gitrepo.credentials_kind(gitrepo.build_secret("c", "u", username="u", password="p")) == "https"
    assert gitrepo.credentials_kind(gitrepo.build_secret("c", "u", ssh_private_key="k")) == "ssh"


@pytest.mark.parametrize(("username", "password"), [("", "p"), ("u", ""), ("u\nx", "p"), ("u", "p\n")])
def test_build_secret_rejects_empty_or_multiline_https_values(username, password):
    with pytest.raises(ValidationError):
        gitrepo.build_secret("c", "https://h/x.git", username=username, password=password)


def test_replace_secret_url():
    manifest = gitrepo.build_secret("c", "git@a:x.git", ssh_private_key="KEY\n")
    doc = yamlio.load(gitrepo.replace_secret_url(manifest, "git@b:y.git").decode())
    assert _b64(doc["data"]["url"]) == "git@b:y.git" and _b64(doc["data"]["sshPrivateKey"]) == "KEY\n"


def test_needs_known_hosts():
    assert not gitrepo.needs_known_hosts(classify_url("git@github.com:me/x.git"))
    assert gitrepo.needs_known_hosts(classify_url("ssh://git@github.com:2222/me/x.git"))
    assert gitrepo.needs_known_hosts(classify_url("git@git.example.com:me/x.git"))
    assert not gitrepo.needs_known_hosts(classify_url("https://git.example.com/me/x.git"))


def test_fingerprint_matches_ssh_keygen(tmp_path):
    line = _host_line(tmp_path, host="git.example.com")
    (tmp_path / "kh").write_text(line + "\n")
    expected = subprocess.run(
        ["ssh-keygen", "-lf", str(tmp_path / "kh")],
        capture_output=True,
        text=True,
        check=True,
    )
    assert gitrepo.fingerprint(line).split()[1] in expected.stdout


def test_ssh_credentials_for_github_skip_keyscan(ctx, spy):
    keyscan = spy("ssh-keyscan")
    result = gitrepo.create_ssh_credentials(
        ctx, "test", classify_url("git@github.com:me/x.git"), confirm=lambda q: False
    )
    assert keyscan.calls == []
    assert result.public_key.startswith("ssh-ed25519 ") and result.public_key.endswith(" talkub-test")
    assert not result.known_hosts_written
    secret = _secret(ctx)
    assert _b64(secret["data"]["url"]) == "git@github.com:me/x.git"
    assert "OPENSSH PRIVATE KEY" in _b64(secret["data"]["sshPrivateKey"])
    for path in ctx.repo.root.rglob("*"):
        if path.is_file():
            assert b"PRIVATE KEY" not in path.read_bytes(), path  # only ciphertext on disk


def test_ssh_credentials_custom_host_confirmed_adds_extra_hosts_once(ctx, spy, tmp_path):
    line = _host_line(tmp_path)
    spy("ssh-keyscan").on(
        "-p",
        "2222",
        "git.example.com",
        stdout=f"# git.example.com:2222 SSH-2.0\n{line}\n",
    )
    url = classify_url("ssh://git@git.example.com:2222/me/x.git")
    questions = []
    result = gitrepo.create_ssh_credentials(ctx, "test", url, confirm=lambda q: questions.append(q) or True)
    assert result.known_hosts_written
    assert gitrepo.fingerprint(line) in questions[0]
    values_path = ctx.repo.gitops_dir / "values" / "argocd.yaml"
    values = yamlio.load(values_path.read_text())
    assert values["configs"]["ssh"]["extraHosts"].splitlines() == [line]
    assert values_path.read_text().startswith("# Helm values for Argo CD\n")  # comment kept
    again = gitrepo.create_ssh_credentials(ctx, "test", url, confirm=lambda q: True)
    assert not again.known_hosts_written
    assert yamlio.load(values_path.read_text())["configs"]["ssh"]["extraHosts"].splitlines() == [line]


def test_ssh_credentials_declined_host_keys_write_nothing(ctx, spy, tmp_path):
    spy("ssh-keyscan").on("git.example.com", stdout=_host_line(tmp_path, host="git.example.com") + "\n")
    with pytest.raises(ValidationError):
        gitrepo.create_ssh_credentials(
            ctx,
            "test",
            classify_url("git@git.example.com:me/x.git"),
            confirm=lambda q: False,
        )
    assert not gitrepo.credentials_path(ctx.repo).exists()
    assert (ctx.repo.gitops_dir / "values" / "argocd.yaml").read_text() == VALUES


def test_ssh_credentials_unreachable_host_fails_before_keygen(ctx, spy):
    spy("ssh-keyscan")  # prints nothing, exit 0 (like a real unreachable host)
    keygen = spy("ssh-keygen")
    with pytest.raises(CommandFailed) as exc:
        gitrepo.create_ssh_credentials(
            ctx,
            "test",
            classify_url("git@git.example.com:me/x.git"),
            confirm=lambda q: True,
        )
    assert "git.example.com" in str(exc.value)
    assert keygen.calls == []
    assert not gitrepo.credentials_path(ctx.repo).exists()


def test_https_credentials(ctx):
    path = gitrepo.create_https_credentials(ctx, "test", classify_url("https://h.example/x.git"), "jam", "tok-123")
    assert path == gitrepo.credentials_path(ctx.repo)
    assert b"tok-123" not in path.read_bytes()
    data = _secret(ctx)["data"]
    assert (_b64(data["username"]), _b64(data["password"])) == ("jam", "tok-123")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_gitrepo_credentials.py -q`
Expected: FAIL (`AttributeError: module 'talkub.gitrepo' has no attribute 'build_secret'`).

- [ ] **Step 3: Implement**

`src/talkub/runner.py` `INSTALL_HINTS`: add `"git": "brew install git"`, `"ssh-keygen": "part of OpenSSH (preinstalled on macOS)"`, `"ssh-keyscan": "part of OpenSSH (preinstalled on macOS)"`.

`src/talkub/gitrepo.py` (new imports: `base64`, `hashlib`, `from collections.abc import Callable`, `from ruamel.yaml.comments import CommentedMap`, `from ruamel.yaml.scalarstring import LiteralScalarString`, `from talkub.context import Context`, `from talkub.ephemeral import private_tempdir`, `from talkub.errors import CommandFailed`, `from talkub.runner import Runner`):

```python
CREDENTIALS_FILE = "repo-credentials.sops.yaml"
SECRET_TYPE_LABEL = "argocd.argoproj.io/secret-type"
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
        ctx.sops.encrypt(manifest, filename=path.name, public_key=ctx.keyring.public_key()),
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
    return url.kind == "ssh" and (url.host not in ARGOCD_KNOWN_HOSTS or url.port is not None)


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


def add_extra_hosts(repo: Repo, lines: list[str]) -> bool:
    """Append known-hosts lines to configs.ssh.extraHosts in gitops/values/argocd.yaml."""
    path = repo.gitops_dir / "values" / "argocd.yaml"
    data = (yamlio.load(path.read_text()) if path.is_file() else None) or CommentedMap()
    ssh = data.setdefault("configs", CommentedMap()).setdefault("ssh", CommentedMap())
    existing = [line for line in str(ssh.get("extraHosts") or "").splitlines() if line.strip()]
    new = [line for line in lines if line not in existing]
    if not new:
        return False
    ssh["extraHosts"] = LiteralScalarString("\n".join(existing + new) + "\n")
    atomic_write(path, yamlio.dump(data).encode())
    return True


@dataclass(frozen=True)
class SshCredentials:
    path: Path
    public_key: str
    known_hosts_written: bool


def create_ssh_credentials(ctx: Context, name: str, url: GitUrl, confirm: Callable[[str], bool]) -> SshCredentials:
    """Host keys are confirmed first; nothing is generated or written if that fails."""
    host_lines: list[str] = []
    if needs_known_hosts(url):
        host_lines = scan_host_keys(ctx.runner, url)
        shown = "\n".join(f"  {fingerprint(line)}" for line in host_lines)
        if not confirm(f"SSH host keys of {url.host}:\n{shown}\nTrust them for Argo CD?"):
            raise ValidationError("host keys not confirmed; nothing was written")
    private, public = generate_ssh_key(ctx.runner, f"talkub-{name}")
    path = write_credentials(ctx, build_secret(name, url.url, ssh_private_key=private))
    written = add_extra_hosts(ctx.repo, host_lines) if host_lines else False
    return SshCredentials(path, public, written)


def create_https_credentials(ctx: Context, name: str, url: GitUrl, username: str, token: str) -> Path:
    return write_credentials(ctx, build_secret(name, url.url, username=username, password=token))
```

Note: `gitrepo` now imports `talkub.context`; `context` does not import `gitrepo`, so there is no cycle. If `CommentedMap.setdefault` on a mapping loaded from YAML returns a plain dict for an existing `configs: {}` flow mapping, it still works (`setdefault` then assignment); the test covers the block-style file written by `init`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_gitrepo_credentials.py tests/test_gitrepo.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/talkub/gitrepo.py src/talkub/runner.py tests/test_gitrepo_credentials.py
git commit -m "feat: Argo CD repository credentials (SSH deploy key, HTTPS token)"
```

---

### Task 4: `set_url`, `apply`, bootstrap integration

**Files:**
- Modify: `src/talkub/gitrepo.py`
- Modify: `src/talkub/bootstrap.py` (the `applying gitops/root.yaml` step)
- Test: `tests/test_gitrepo_flows.py` (new), `tests/test_bootstrap.py`

**Interfaces:**
- Consumes: everything from Tasks 1–3; `talkub.kube.KubeSession` (`kubectl(args)`); `talkub.ephemeral.decrypted`; `GitSpec`.
- Produces (in `talkub.gitrepo`):
  - `@dataclass(frozen=True) SetUrlResult(changed: list[Path], credentials_updated: bool, unchanged: bool)`
  - `set_url(ctx: Context, url: str) -> SetUrlResult`
  - `apply(ctx: Context, kube: KubeSession) -> None`
  - `placeholder_warning(repo: Repo) -> str | None`

- [ ] **Step 1: Write the failing tests**

`tests/test_gitrepo_flows.py`:

```python
from string import Template

import pytest

from helpers import need
from talkub import gitrepo, yamlio
from talkub.context import Context
from talkub.errors import ValidationError
from talkub.fsutil import atomic_write
from talkub.kube import kube_session
from talkub.repo import GitSpec
from talkub.scaffold import TEMPLATES

pytestmark = need("age", "age-keygen", "sops")

PLACEHOLDER = "https://example.invalid/CHANGE-ME/test.git"
SSH_URL = "git@github.com:me/test.git"


@pytest.fixture
def ctx(repo_with_configs):
    for rel in (
        "gitops/root.yaml",
        "gitops/apps/argocd.yaml",
        "gitops/apps/sealed-secrets.yaml",
    ):
        template = (TEMPLATES / "repo" / (rel + ".tmpl")).read_text()
        text = Template(template).safe_substitute({"git_url": PLACEHOLDER, "name": "test"})
        atomic_write(repo_with_configs.root / rel, text.encode())
    return Context.open(repo_with_configs)


def _creds(ctx, url, **kind):
    gitrepo.write_credentials(ctx, gitrepo.build_secret("test", url, **kind))


def test_set_url_from_placeholder_without_git_block(ctx):
    result = gitrepo.set_url(ctx, SSH_URL)
    assert len(result.changed) == 3 and not result.credentials_updated and not result.unchanged
    assert ctx.repo.load_settings().git == GitSpec(SSH_URL)
    assert PLACEHOLDER not in (ctx.repo.gitops_dir / "root.yaml").read_text()


def test_set_url_again_is_a_no_op(ctx):
    gitrepo.set_url(ctx, SSH_URL)
    result = gitrepo.set_url(ctx, SSH_URL)
    assert result.unchanged and result.changed == []


def test_set_url_from_stored_url_updates_credentials(ctx):
    gitrepo.set_url(ctx, SSH_URL)
    _creds(ctx, SSH_URL, ssh_private_key="KEY\n")
    result = gitrepo.set_url(ctx, "git@gitlab.com:me/test.git")
    assert result.credentials_updated and len(result.changed) == 3
    secret = yamlio.load(ctx.sops.decrypt(gitrepo.credentials_path(ctx.repo)).decode())
    import base64

    assert base64.b64decode(secret["data"]["url"]).decode() == "git@gitlab.com:me/test.git"


def test_set_url_kind_mismatch_changes_nothing(ctx):
    gitrepo.set_url(ctx, SSH_URL)
    _creds(ctx, SSH_URL, ssh_private_key="KEY\n")
    before = {p: p.read_bytes() for p in ctx.repo.root.rglob("*") if p.is_file()}
    with pytest.raises(ValidationError) as exc:
        gitrepo.set_url(ctx, "https://github.com/me/test.git")
    assert "--remove" in (exc.value.hint or "")
    assert {p: p.read_bytes() for p in ctx.repo.root.rglob("*") if p.is_file()} == before


def test_set_url_without_git_block_and_no_placeholder_match_fails(ctx):
    root = ctx.repo.gitops_dir / "root.yaml"
    root.write_text(root.read_text().replace(PLACEHOLDER, "https://hand.example/x.git"))
    for app in (ctx.repo.gitops_dir / "apps").iterdir():
        app.write_text(app.read_text().replace(PLACEHOLDER, "https://hand.example/x.git"))
    with pytest.raises(ValidationError):
        gitrepo.set_url(ctx, SSH_URL)
    assert ctx.repo.load_settings().git is None


def test_set_url_rejects_bad_url_before_touching_files(ctx):
    before = (ctx.repo.gitops_dir / "root.yaml").read_text()
    with pytest.raises(ValidationError):
        gitrepo.set_url(ctx, "http://github.com/me/x.git")
    assert (ctx.repo.gitops_dir / "root.yaml").read_text() == before


def test_apply_credentials_then_root(ctx, spy):
    kubectl = spy("kubectl")
    _creds(ctx, SSH_URL, ssh_private_key="KEY\n")
    with kube_session(ctx) as kube:
        gitrepo.apply(ctx, kube)
    first, second = kubectl.calls
    assert first["args"][:3] == ["apply", "--server-side", "-f"] and first["files"]["-f"]
    assert second["args"] == ["apply", "-f", str(ctx.repo.gitops_dir / "root.yaml")]


def test_apply_without_credentials_only_root(ctx, spy):
    kubectl = spy("kubectl")
    with kube_session(ctx) as kube:
        gitrepo.apply(ctx, kube)
    assert kubectl.argvs == [["apply", "-f", str(ctx.repo.gitops_dir / "root.yaml")]]


def test_placeholder_warning(ctx):
    assert "talkub repo set-url" in gitrepo.placeholder_warning(ctx.repo)
    gitrepo.set_url(ctx, SSH_URL)
    assert gitrepo.placeholder_warning(ctx.repo) is None
```

In `tests/test_bootstrap.py`, add:

```python
def test_bootstrap_applies_repo_credentials_before_root(cluster):
    from talkub import gitrepo

    repo, spies = cluster
    ctx = Context.open(repo)
    gitrepo.write_credentials(
        ctx,
        gitrepo.build_secret("test", "git@github.com:me/x.git", ssh_private_key="K\n"),
    )
    bootstrap.run(ctx)
    applies = [c for c in spies["kubectl"].calls if c["args"][0] == "apply"]
    root = [i for i, c in enumerate(applies) if c["args"] == ["apply", "-f", str(repo.gitops_dir / "root.yaml")]]
    creds = [i for i, c in enumerate(applies) if c["args"][:2] == ["apply", "--server-side"]]
    assert root and creds and max(creds) < root[0]
```

(The `cluster` fixture has no sealing-key backup, so the only `--server-side` apply is the credentials one.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_gitrepo_flows.py tests/test_bootstrap.py -q`
Expected: FAIL (`AttributeError: ... set_url`; bootstrap test finds no `--server-side` apply).

- [ ] **Step 3: Implement**

`src/talkub/gitrepo.py` (imports: `from talkub.ephemeral import decrypted`, `from talkub.kube import KubeSession`, `from talkub.repo import GitSpec, Repo`):

```python
@dataclass(frozen=True)
class SetUrlResult:
    changed: list[Path]
    credentials_updated: bool
    unchanged: bool


def set_url(ctx: Context, url: str) -> SetUrlResult:
    """Point settings, gitops repoURLs and stored credentials at `url`. Never touches the cluster."""
    new = classify_url(url)
    settings = ctx.repo.load_settings()
    old = settings.git.url if settings.git is not None else placeholder_url(settings.name)
    if old == new.url:
        return SetUrlResult([], False, True)

    creds = credentials_path(ctx.repo)
    manifest = ctx.sops.decrypt(creds) if creds.is_file() else None
    if manifest is not None and credentials_kind(manifest) != new.kind:
        raise ValidationError(
            f"the stored repo credentials are for {credentials_kind(manifest)} URLs, the new URL is {new.kind}",
            hint="talkub repo credentials --remove, then set-url, then talkub repo credentials",
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
    return SetUrlResult(changed, manifest is not None, False)


def apply(ctx: Context, kube: KubeSession) -> None:
    """Repo credentials first (server-side: no last-applied annotation with the key), then the root app."""
    creds = credentials_path(ctx.repo)
    if creds.is_file():
        with decrypted(ctx.sops, {"repo-credentials.yaml": creds}) as plain:
            kube.kubectl(["apply", "--server-side", "-f", str(plain["repo-credentials.yaml"])])
    kube.kubectl(["apply", "-f", str(ctx.repo.gitops_dir / "root.yaml")])


def placeholder_warning(repo: Repo) -> str | None:
    root = repo.gitops_dir / "root.yaml"
    if root.is_file() and "example.invalid/CHANGE-ME" in root.read_text():
        return "gitops/root.yaml still points at the placeholder URL; run `talkub repo set-url <url>`"
    return None
```

Check for an import cycle: `kube` imports `context`; neither imports `gitrepo`. If `bootstrap` → `gitrepo` → `kube` creates a cycle anywhere, import `KubeSession` under `TYPE_CHECKING` only.

`src/talkub/bootstrap.py`: add `from talkub import gitrepo` and replace

```python
        _step("applying gitops/root.yaml")
        kube.kubectl(["apply", "-f", str(ctx.repo.gitops_dir / "root.yaml")])
```

with

```python
        _step("applying the repo credentials and gitops/root.yaml")
        if warning := gitrepo.placeholder_warning(ctx.repo):
            print(f"   warning: {warning}", flush=True)
        gitrepo.apply(ctx, kube)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_gitrepo_flows.py tests/test_bootstrap.py -q`
Expected: PASS (existing bootstrap tests still see `["apply", "-f", ".../root.yaml"]`).

- [ ] **Step 5: Commit**

```bash
git add src/talkub/gitrepo.py src/talkub/bootstrap.py tests/test_gitrepo_flows.py tests/test_bootstrap.py
git commit -m "feat: repo set-url and apply flows; bootstrap applies repo credentials"
```

---

### Task 5: `talkub repo` CLI group

**Files:**
- Create: `src/talkub/cli/repo.py`
- Create: `src/talkub/cli/prompts.py` (shared secret input)
- Modify: `src/talkub/cli/tailscale.py` (`read_authkey` delegates to `prompts.read_secret`)
- Modify: `src/talkub/cli/main.py` (register `repo`)
- Test: `tests/test_cli_repo.py` (new)

**Interfaces:**
- Consumes: `gitrepo.set_url`, `SetUrlResult`, `credentials_path`, `classify_url`, `create_ssh_credentials`, `create_https_credentials`, `apply`, `placeholder_warning`; `talkub.kube.kube_session`; `ctx.obj.context()` (CLI state).
- Produces: `talkub.cli.prompts.read_secret(path: Path | None, prompt: str, repo_root: Path | None = None) -> str`; `talkub.cli.tailscale.read_authkey(path, repo_root=None)` keeps its signature and its "must not be committed" warning.

- [ ] **Step 1: Write the failing tests**

`tests/test_cli_repo.py`:

```python
from string import Template

import pytest

from helpers import need
from talkub import gitrepo
from talkub.cli.main import main
from talkub.context import Context
from talkub.fsutil import atomic_write
from talkub.scaffold import TEMPLATES

pytestmark = need("age", "age-keygen", "sops", "ssh-keygen")

PLACEHOLDER = "https://example.invalid/CHANGE-ME/test.git"


def _run(argv):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    return exc.value.code


@pytest.fixture
def repo(repo_with_configs):
    for rel in (
        "gitops/root.yaml",
        "gitops/apps/argocd.yaml",
        "gitops/apps/sealed-secrets.yaml",
        "gitops/values/argocd.yaml",
    ):
        template = (
            (TEMPLATES / "repo" / (rel + ".tmpl"))
            if (TEMPLATES / "repo" / (rel + ".tmpl")).is_file()
            else (TEMPLATES / "repo" / rel)
        )
        text = Template(template.read_text()).safe_substitute({"git_url": PLACEHOLDER, "name": "test"})
        atomic_write(repo_with_configs.root / rel, text.encode())
    return repo_with_configs


def test_set_url_prints_files_and_next_step(repo, capfd):
    assert _run(["--repo", str(repo.root), "repo", "set-url", "git@github.com:me/test.git"]) == 0
    out = capfd.readouterr().out
    assert "updated gitops/root.yaml" in out and "talkub repo apply" in out
    assert _run(["--repo", str(repo.root), "repo", "set-url", "git@github.com:me/test.git"]) == 0
    assert "already set" in capfd.readouterr().out


def test_credentials_needs_git_url(repo, capfd):
    assert _run(["--repo", str(repo.root), "repo", "credentials"]) == 1
    assert "talkub repo set-url" in capfd.readouterr().err


def test_ssh_credentials_print_public_key(repo, capfd):
    _run(["--repo", str(repo.root), "repo", "set-url", "git@github.com:me/test.git"])
    capfd.readouterr()
    assert _run(["--repo", str(repo.root), "repo", "credentials"]) == 0
    out = capfd.readouterr().out
    assert "ssh-ed25519 " in out and "deploy key" in out and "PRIVATE" not in out


def test_https_credentials_from_token_file(repo, tmp_path, capfd):
    _run(
        [
            "--repo",
            str(repo.root),
            "repo",
            "set-url",
            "https://git.example.com/me/test.git",
        ]
    )
    token = tmp_path / "token"
    token.write_text("tok-abc\n")
    code = _run(
        [
            "--repo",
            str(repo.root),
            "repo",
            "credentials",
            "--username",
            "jam",
            "--token-file",
            str(token),
        ]
    )
    assert code == 0
    assert "tok-abc" not in capfd.readouterr().out
    assert gitrepo.credentials_path(repo).is_file()


def test_ssh_url_rejects_https_options(repo, tmp_path, capfd):
    _run(["--repo", str(repo.root), "repo", "set-url", "git@github.com:me/test.git"])
    assert _run(["--repo", str(repo.root), "repo", "credentials", "--username", "jam"]) == 1


def test_existing_credentials_ask_before_replacing(repo, monkeypatch, capfd):
    _run(["--repo", str(repo.root), "repo", "set-url", "git@github.com:me/test.git"])
    _run(["--repo", str(repo.root), "repo", "credentials"])
    before = gitrepo.credentials_path(repo).read_bytes()
    monkeypatch.setattr("typer.confirm", lambda *a, **k: False)
    assert _run(["--repo", str(repo.root), "repo", "credentials"]) == 1
    assert gitrepo.credentials_path(repo).read_bytes() == before


def test_remove_credentials(repo, capfd):
    _run(["--repo", str(repo.root), "repo", "set-url", "git@github.com:me/test.git"])
    _run(["--repo", str(repo.root), "repo", "credentials"])
    capfd.readouterr()
    assert _run(["--repo", str(repo.root), "repo", "credentials", "--remove"]) == 0
    assert not gitrepo.credentials_path(repo).exists()
    assert "delete secret repo-test" in capfd.readouterr().out


def test_apply_warns_about_placeholder(repo, spy, capfd):
    kubectl = spy("kubectl")
    assert _run(["--repo", str(repo.root), "repo", "apply"]) == 0
    assert "placeholder" in capfd.readouterr().err
    assert kubectl.argvs == [["apply", "-f", str(repo.gitops_dir / "root.yaml")]]
```

Check how existing CLI tests exit on success (`main` raising `SystemExit(0)`); adapt `_run` exactly like `tests/test_cli_tailscale.py`. If `typer.confirm` is imported into the module namespace instead of used as `typer.confirm`, patch `talkub.cli.repo.typer.confirm` accordingly.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cli_repo.py tests/test_cli_tailscale.py -q`
Expected: FAIL (`No such command 'repo'`).

- [ ] **Step 3: Implement**

`src/talkub/cli/prompts.py`:

```python
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
```

`src/talkub/cli/tailscale.py`: replace the body of `read_authkey` with `return read_secret(path, "Tailscale auth key: ", repo_root)` (import `from talkub.cli.prompts import read_secret`; drop now-unused imports). The existing tests assert "must not be committed" and the file name in the warning — both still hold.

`src/talkub/cli/repo.py`:

```python
"""repo set-url, repo credentials, repo apply."""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from talkub import gitrepo
from talkub.cli.prompts import read_secret
from talkub.errors import ValidationError
from talkub.kube import kube_session

app = typer.Typer(help="The git repo Argo CD pulls from: URL and credentials.", no_args_is_help=True)
NEXT = "next: commit, push, then `talkub repo apply`"


@app.command("set-url")
def set_url(ctx: typer.Context, url: str = typer.Argument(..., help="SSH or HTTPS git URL.")) -> None:
    """Store the git URL and point the gitops/ repoURLs at it (files only)."""
    context = ctx.obj.context()
    result = gitrepo.set_url(context, url)
    if result.unchanged:
        print("git URL already set; nothing to do")
        return
    for path in result.changed:
        print(f"updated {context.repo.rel(path)}")
    if result.credentials_updated:
        print(f"updated {context.repo.rel(gitrepo.credentials_path(context.repo))}")
    print(NEXT)


@app.command("credentials")
def credentials(
    ctx: typer.Context,
    username: str = typer.Option(None, "--username", help="HTTPS user name (default: prompt)."),
    token_file: Path = typer.Option(None, "--token-file", help="File with the HTTPS token (default: hidden prompt)."),
    remove: bool = typer.Option(
        False,
        "--remove",
        help="Delete the stored credentials (the cluster keeps its copy).",
    ),
) -> None:
    """Create Argo CD credentials for the repo: SSH deploy key or HTTPS token."""
    context = ctx.obj.context()
    repo = context.repo
    path = gitrepo.credentials_path(repo)
    settings = repo.load_settings()
    if remove:
        if not path.exists():
            print("no repo credentials stored")
            return
        path.unlink()
        print(f"removed {repo.rel(path)}")
        print(f"note: the cluster keeps its copy; `talkub k -n argocd delete secret repo-{settings.name}` removes it")
        return
    if settings.git is None:
        raise ValidationError("no git URL in .talkub.yaml", hint="talkub repo set-url <url>")
    url = gitrepo.classify_url(settings.git.url)
    if url.kind == "ssh" and (username or token_file):
        raise ValidationError("--username/--token-file are for https URLs")
    if path.exists() and not typer.confirm("Replace the existing repo credentials?", default=False):
        raise typer.Exit(1)
    if url.kind == "ssh":
        result = gitrepo.create_ssh_credentials(
            context,
            settings.name,
            url,
            confirm=lambda question: typer.confirm(question, default=False),
        )
        print(f"wrote {repo.rel(result.path)}")
        if result.known_hosts_written:
            print("updated gitops/values/argocd.yaml (configs.ssh.extraHosts)")
        print("add this public key as a read-only deploy key to the repo:")
        print(result.public_key)
    else:
        user = username or typer.prompt("User name")
        token = read_secret(token_file, "Token: ", repo_root=repo.root)
        print(f"wrote {repo.rel(gitrepo.create_https_credentials(context, settings.name, url, user, token))}")
    print(NEXT)


@app.command("apply")
def apply(ctx: typer.Context) -> None:
    """Apply the repo credentials and gitops/root.yaml to the cluster."""
    context = ctx.obj.context()
    if warning := gitrepo.placeholder_warning(context.repo):
        print(f"warning: {warning}", file=sys.stderr)
    with kube_session(context) as kube:
        gitrepo.apply(context, kube)
    print("applied the repo credentials (if any) and gitops/root.yaml")
```

`src/talkub/cli/main.py`: `from talkub.cli import repo as repo_cli` and `app.add_typer(repo_cli.app, name="repo")` after the tailscale line.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cli_repo.py tests/test_cli_tailscale.py -q`
Expected: PASS. Then `uv run pytest -q`.

- [ ] **Step 5: Commit**

```bash
git add src/talkub/cli/repo.py src/talkub/cli/prompts.py src/talkub/cli/tailscale.py src/talkub/cli/main.py tests/test_cli_repo.py
git commit -m "feat: talkub repo set-url/credentials/apply"
```

---

### Task 6: `init` writes `git.url` and runs `git init`; `check` finds plaintext repo secrets

**Files:**
- Modify: `src/talkub/gitrepo.py` (`git_init`)
- Modify: `src/talkub/scaffold.py` (`_validate` uses `classify_url`; `Settings(..., git=GitSpec(request.git_url))`)
- Modify: `src/talkub/cli/setup.py` (call `git_init` after `init_cluster`; final message)
- Modify: `src/talkub/checks.py` (plaintext repository Secret under `bootstrap/`)
- Test: `tests/test_gitrepo.py`, `tests/test_scaffold.py`, `tests/test_cli_manage.py`, `tests/test_checks.py`

**Interfaces:**
- Consumes: `classify_url`, `SECRET_TYPE_LABEL`, `GitSpec`, `Runner`.
- Produces: `gitrepo.git_init(root: Path, url: str, runner: Runner) -> list[str]` (warnings; never raises for git problems).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_gitrepo.py`:

```python
import shutil
import subprocess

from talkub.runner import Runner


@pytest.mark.skipif(shutil.which("git") is None, reason="missing tools: git")
def test_git_init_creates_repo_and_origin(tmp_path):
    assert gitrepo.git_init(tmp_path, NEW, Runner()) == []
    remote = subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
    )
    assert remote.stdout.strip() == NEW
    branch = subprocess.run(
        ["git", "-C", str(tmp_path), "symbolic-ref", "--short", "HEAD"],
        capture_output=True,
        text=True,
    )
    assert branch.stdout.strip() == "main"
    assert gitrepo.git_init(tmp_path, "git@other:x.git", Runner()) == []  # existing origin is kept
    remote = subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
    )
    assert remote.stdout.strip() == NEW


@pytest.mark.skipif(shutil.which("git") is None, reason="missing tools: git")
def test_git_init_on_broken_git_dir_only_warns(tmp_path):
    (tmp_path / ".git").mkdir()  # not a real repository
    warnings = gitrepo.git_init(tmp_path, NEW, Runner())
    assert warnings and "origin" in warnings[0]


def test_git_init_without_git_warns(tmp_path, monkeypatch):
    monkeypatch.setattr(gitrepo.shutil, "which", lambda name: None)
    [warning] = gitrepo.git_init(tmp_path, NEW, Runner())
    assert "git init -b main" in warning
```

In `tests/test_scaffold.py`, extend `test_init_creates_complete_repo` with:

```python
    from talkub.repo import GitSpec

    assert settings.git == GitSpec(url="https://git.example.com/demo.git")
```

and add:

```python
def test_init_rejects_invalid_git_url(tmp_path, software_identity):
    request = _request(tmp_path / "demo", software_identity[1])
    from dataclasses import replace

    with pytest.raises(ValidationError):
        init_cluster(replace(request, git_url="http://git.example.com/demo.git"), Runner())
    assert not (tmp_path / "demo").exists()
```

In `tests/test_cli_manage.py`, in the existing successful `init` test (the one running `init demo ... --git-url https://git.example.com/demo.git`), after `assert code == 0` add:

```python
import shutil as _shutil
import subprocess as _subprocess

if _shutil.which("git"):
    remote = _subprocess.run(
        ["git", "-C", str(tmp_path / "demo"), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
    )
    assert remote.stdout.strip() == "https://git.example.com/demo.git"
```

In `tests/test_checks.py` add (reuse that file's existing fixture for a checked repo; look at how other tests there build a repo that passes `run_checks` and use the same one — named `checked_repo` below):

```python
def test_plaintext_repo_secret_in_bootstrap_is_an_error(checked_repo):
    from talkub import gitrepo
    from talkub.checks import run_checks

    target = checked_repo.root / "bootstrap" / "repo.yaml"
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(gitrepo.build_secret("demo", "git@github.com:me/x.git", ssh_private_key="K\n"))
    messages = [f.message for f in run_checks(checked_repo)]
    assert any("bootstrap/repo.yaml" in m and "plaintext" in m for m in messages)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_gitrepo.py tests/test_scaffold.py tests/test_cli_manage.py tests/test_checks.py -q`
Expected: FAIL (`AttributeError: ... git_init`, `settings.git is None`, no finding).

- [ ] **Step 3: Implement**

`src/talkub/gitrepo.py` (add `import shutil`):

```python
def git_init(root: Path, url: str, runner: Runner) -> list[str]:
    """`git init -b main` and `origin` = url where missing. Problems become warnings, not errors."""
    if shutil.which("git") is None:
        return [f"git not found; run `git init -b main && git remote add origin {url}` in {root}"]
    if not (root / ".git").exists():
        proc = runner.run(["git", "init", "-q", "-b", "main"], cwd=root, check=False)
        if proc.returncode != 0:
            return [f"git init failed: {(proc.stderr or b'').decode(errors='replace').strip()}"]
    if runner.run(["git", "remote", "get-url", "origin"], cwd=root, check=False).returncode != 0:
        proc = runner.run(["git", "remote", "add", "origin", url], cwd=root, check=False)
        if proc.returncode != 0:
            return [f"could not add remote origin: {(proc.stderr or b'').decode(errors='replace').strip()}"]
    return []
```

`src/talkub/scaffold.py`: in `_validate` add `classify_url(request.git_url)` (import `from talkub.gitrepo import classify_url`); in `_populate` pass `git=GitSpec(url=request.git_url.strip())` to `Settings(...)` (import `GitSpec` from `talkub.repo`). Keep `_template_values(settings, request.git_url)` but pass the stripped URL.

`src/talkub/cli/setup.py`, directly after `repo = init_cluster(request, Runner())`:

```python
    for warning in gitrepo.git_init(repo.root, git_url.strip(), Runner()):
        print(f"warning: {warning}", file=sys.stderr)
```

(import `from talkub import gitrepo` and `sys` if missing) and change the final line to:

```python
print("next: add a second admin or backup key (talkub admin add), then git add -A && git commit")
```

`src/talkub/checks.py`, in `run_checks` before the `AGE-SECRET-KEY` loop (import `SECRET_TYPE_LABEL` from `talkub.gitrepo`; if that creates an import cycle, define the literal string locally with a comment pointing at `gitrepo.SECRET_TYPE_LABEL`):

```python
bootstrap_dir = repo.root / "bootstrap"
if bootstrap_dir.is_dir():
    for path in sorted(bootstrap_dir.iterdir()):
        if path.name.endswith(".sops.yaml") or path.suffix not in (".yaml", ".yml") or not path.is_file():
            continue  # *.sops.yaml are checked for encryption above
        if SECRET_TYPE_LABEL.encode() in path.read_bytes():
            findings.append(
                Finding(
                    "error",
                    f"{repo.rel(path)} is a plaintext Argo CD repository secret",
                )
            )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/talkub/gitrepo.py src/talkub/scaffold.py src/talkub/cli/setup.py src/talkub/checks.py tests/
git commit -m "feat: init stores the git URL and runs git init; check flags plaintext repo secrets"
```
