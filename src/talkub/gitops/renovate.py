"""`talkub renovate init`: a renovate.json that opens PRs for chart, Talos and Kubernetes updates.

Chart versions live in `gitops/apps/*.yaml` (the argocd manager); Talos and Kubernetes versions
live in `.talkub.yaml` and are tracked by two custom regex managers, since no built-in manager
knows that file's shape. Argo CD rolls out a merged chart PR immediately, and merging a Talos or
Kubernetes PR does not itself touch the nodes — see the notes below, printed by the CLI.
"""

from __future__ import annotations

import json
from pathlib import Path

from talkub.config import Repo
from talkub.core.errors import ValidationError
from talkub.core.fsutil import atomic_write

FILENAME = "renovate.json"

CONFIG: dict = {
    "$schema": "https://docs.renovatebot.com/renovate-schema.json",
    "extends": ["config:recommended"],
    "argocd": {"managerFilePatterns": ["/^gitops/apps/.+\\.ya?ml$/"]},
    "customManagers": [
        {
            "customType": "regex",
            "managerFilePatterns": ["/^\\.talkub\\.yaml$/"],
            "matchStrings": ['talosVersion:\\s*"?(?<currentValue>v[0-9.]+)"?'],
            "datasourceTemplate": "github-releases",
            "depNameTemplate": "siderolabs/talos",
        },
        {
            "customType": "regex",
            "managerFilePatterns": ["/^\\.talkub\\.yaml$/"],
            "matchStrings": ['kubernetesVersion:\\s*"?(?<currentValue>[0-9.]+)"?'],
            "datasourceTemplate": "github-releases",
            "depNameTemplate": "kubernetes/kubernetes",
            "extractVersionTemplate": "^v(?<version>.*)$",
        },
    ],
    "packageRules": [
        {
            "matchFileNames": [".talkub.yaml"],
            "prBodyNotes": [
                (
                    "After merging, run `talkub upgrade talos` or `talkub upgrade k8s` — "
                    "merging alone does not change the nodes."
                ),
            ],
        }
    ],
}

NOTES = [
    "enable the Renovate GitHub app for this repo: https://github.com/apps/renovate",
    ("chart PRs are rolled out by Argo CD as soon as they merge — run `talkub lint` on the PR branch first"),
    (
        "Talos/Kubernetes PRs need `talkub upgrade talos` or `talkub upgrade k8s` after merge — "
        "merging alone does not change the nodes"
    ),
]


def render() -> str:
    """The renovate.json contents: `CONFIG` as JSON, 2-space indent, trailing newline."""
    return json.dumps(CONFIG, indent=2, ensure_ascii=False) + "\n"


def init(repo: Repo, *, force: bool = False) -> Path:
    """Write renovate.json in the repo root.

    Raises `ValidationError` if the file already exists, unless `force` is set.
    """
    path = repo.root / FILENAME
    if path.is_file() and not force:
        raise ValidationError(f"{FILENAME} already exists", hint="talkub renovate init --force")
    atomic_write(path, render().encode())
    return path
