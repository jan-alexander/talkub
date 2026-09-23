"""`talkub check`: guard rails before committing a cluster repo."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ruamel.yaml.error import YAMLError

from talkub import recipients as recipients_file
from talkub import yamlio
from talkub.errors import ValidationError
from talkub.repo import Repo

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


def run_checks(repo: Repo) -> list[Finding]:
    findings: list[Finding] = []

    for path in repo.sops_files():
        try:
            data = yamlio.load(path.read_text())
        except YAMLError:
            data = None
        if not isinstance(data, Mapping) or "sops" not in data:
            findings.append(Finding("error", f"{repo.rel(path)} is not SOPS-encrypted"))

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
                    Finding("error", f"{repo.rel(path)} contains a plain Secret; seal it with kubeseal")
                )

    for path in _repo_files(repo):
        if b"AGE-SECRET-KEY-" in path.read_bytes():
            findings.append(Finding("error", f"{repo.rel(path)} contains an age private key"))

    for rel in PLAINTEXT_NAMES:
        if (repo.root / rel).exists():
            findings.append(Finding("error", f"{rel} is plaintext and must not be in the repo"))

    try:
        count = len(recipients_file.parse(repo.recipients_file.read_text()))
    except (OSError, ValidationError):
        findings.append(Finding("error", "keys/recipients.txt is missing or invalid"))
    else:
        if count < 2:
            findings.append(
                Finding("warning", f"only {count} recipient(s); add a second admin or backup key")
            )

    if repo.master_next_age.exists():
        findings.append(Finding("warning", "keys/master.next.age exists: a key rotation was interrupted"))

    return findings
