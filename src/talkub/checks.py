"""`talkub check`: guard rails before committing a cluster repo."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ruamel.yaml.error import YAMLError

from talkub.argoapps import SECRET_TYPE_LABEL
from talkub.config.repo import Repo
from talkub.core import yamlio
from talkub.core.errors import ValidationError
from talkub.secrets import recipients as recipients_file

PLAINTEXT_NAMES = ("talos/secrets.yaml", "talos/talosconfig", "kubernetes/kubeconfig")


@dataclass(frozen=True)
class Finding:
    level: str  # "error" or "warning"
    message: str


def _repo_files(repo: Repo) -> list[Path]:
    return [
        path
        for path in repo.root.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(repo.root).parts
    ]


def _load_yaml(path: Path) -> object:
    try:
        return yamlio.load(path.read_text())
    except (OSError, YAMLError):
        return None


def _age_recipients(metadata: object) -> set[str]:
    """Recipients from a SOPS file's `sops.age` block."""
    entries = metadata.get("age") if isinstance(metadata, Mapping) else None
    if not isinstance(entries, list):
        return set()
    return {
        str(e["recipient"]).strip()
        for e in entries
        if isinstance(e, Mapping) and "recipient" in e
    }


def _sops_config_keys(config: object) -> set[str]:
    """age keys named in `.sops.yaml` creation rules (a comma-separated string or a list)."""
    rules = config.get("creation_rules") if isinstance(config, Mapping) else None
    keys: set[str] = set()
    for rule in rules if isinstance(rules, list) else []:
        value = rule.get("age") if isinstance(rule, Mapping) else None
        items = value if isinstance(value, list) else str(value or "").split(",")
        keys.update(str(item).strip() for item in items if str(item).strip())
    return keys


def _master_pub(repo: Repo, findings: list[Finding]) -> str | None:
    try:
        return repo.master_pub.read_text().strip() or None
    except OSError:
        findings.append(Finding("error", "keys/master.pub is missing"))
        return None


def run_checks(repo: Repo) -> list[Finding]:
    """Key-free checks: nothing here decrypts or needs an identity."""
    findings: list[Finding] = []
    master_pub = _master_pub(repo, findings)

    if master_pub is not None:
        if not repo.sops_config.is_file():
            findings.append(Finding("error", ".sops.yaml is missing"))
        elif master_pub not in _sops_config_keys(_load_yaml(repo.sops_config)):
            findings.append(
                Finding("error", ".sops.yaml does not use the key in keys/master.pub")
            )

    for path in repo.sops_files():
        data = _load_yaml(path)
        if not isinstance(data, Mapping) or "sops" not in data:
            findings.append(Finding("error", f"{repo.rel(path)} is not SOPS-encrypted"))
        elif master_pub is not None and master_pub not in _age_recipients(data["sops"]):
            findings.append(
                Finding(
                    "error", f"{repo.rel(path)} is not encrypted to keys/master.pub"
                )
            )

    if repo.gitops_dir.is_dir():
        for path in sorted(repo.gitops_dir.rglob("*")):
            if path.suffix not in (".yaml", ".yml") or not path.is_file():
                continue
            try:
                docs = yamlio.load_all(path.read_text())
            except YAMLError:
                findings.append(Finding("error", f"{repo.rel(path)} is not valid YAML"))
                continue
            if any(isinstance(d, Mapping) and d.get("kind") == "Secret" for d in docs):
                findings.append(
                    Finding(
                        "error",
                        f"{repo.rel(path)} contains a plain Secret; seal it with kubeseal",
                    )
                )

    bootstrap_dir = repo.root / "bootstrap"
    if bootstrap_dir.is_dir():
        for path in sorted(bootstrap_dir.iterdir()):
            if (
                path.name.endswith(".sops.yaml")
                or path.suffix not in (".yaml", ".yml")
                or not path.is_file()
            ):
                continue  # *.sops.yaml are checked for encryption above
            if SECRET_TYPE_LABEL.encode() in path.read_bytes():
                findings.append(
                    Finding(
                        "error",
                        f"{repo.rel(path)} is a plaintext Argo CD repository secret",
                    )
                )

    for path in _repo_files(repo):
        if b"AGE-SECRET-KEY-" in path.read_bytes():
            findings.append(
                Finding("error", f"{repo.rel(path)} contains an age private key")
            )

    for rel in PLAINTEXT_NAMES:
        if (repo.root / rel).exists():
            findings.append(
                Finding("error", f"{rel} is plaintext and must not be in the repo")
            )

    try:
        count = len(recipients_file.parse(repo.recipients_file.read_text()))
    except (OSError, ValidationError):
        findings.append(Finding("error", "keys/recipients.txt is missing or invalid"))
    else:
        if count < 2:
            findings.append(
                Finding(
                    "warning",
                    f"only {count} recipient(s); add a second admin or backup key",
                )
            )

    if repo.master_next_age.exists():
        findings.append(
            Finding(
                "warning", "keys/master.next.age exists: a key rotation was interrupted"
            )
        )

    return findings
