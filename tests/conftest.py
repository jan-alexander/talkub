"""Shared fixtures. talkub imports are lazy so early tasks run before later modules exist."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from helpers import sample_settings


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    """Never touch the developer's real keys or kube/talos configs."""
    for var in (
        "SOPS_AGE_KEY",
        "SOPS_AGE_KEY_FILE",
        "TALKUB_IDENTITY",
        "TALOSCONFIG",
        "KUBECONFIG",
        "TALKUB_SESSION",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))


@pytest.fixture
def fake_bin(tmp_path, monkeypatch):
    """Install a fake executable (POSIX sh script) in front of PATH."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    def install(name: str, script: str) -> Path:
        path = bindir / name
        path.write_text("#!/bin/sh\n" + script)
        path.chmod(0o755)
        return path

    return install


def _require(*tools: str) -> None:
    missing = [tool for tool in tools if shutil.which(tool) is None]
    if missing:
        pytest.skip(f"missing tools: {', '.join(missing)}")


@pytest.fixture
def software_identity():
    """A software age identity in the isolated default identities dir: (path, public key)."""
    _require("age-keygen")
    from talkub.identity import create_identity, identities_dir
    from talkub.runner import Runner

    return create_identity(Runner(), "software", identities_dir(os.environ))


@pytest.fixture
def empty_repo(tmp_path):
    """A directory with a valid .talkub.yaml and nothing else."""
    from talkub.repo import Repo

    root = tmp_path / "cluster"
    root.mkdir()
    repo = Repo(root)
    repo.write_settings(sample_settings())
    return repo


@pytest.fixture
def keyed_repo(empty_repo, software_identity):
    """empty_repo plus a master key encrypted to software_identity (recipient 'me')."""
    _require("age")
    from talkub.keyring import Keyring
    from talkub.recipients import Recipient
    from talkub.runner import Runner

    Keyring(empty_repo, Runner()).create([Recipient("me", software_identity[1])])
    return empty_repo


@pytest.fixture
def repo_with_configs(keyed_repo):
    """keyed_repo plus .sops.yaml and SOPS-encrypted talosconfig and kubeconfig with marker content."""
    _require("sops")
    from talkub.context import Context
    from talkub.crypto.sops import render_sops_config
    from talkub.fsutil import atomic_write

    ctx = Context.open(keyed_repo)
    public = ctx.keyring.public_key()
    atomic_write(keyed_repo.sops_config, render_sops_config(public).encode())
    for path, text in (
        (keyed_repo.talosconfig, b"talos: marker\n"),
        (keyed_repo.kubeconfig, b"kube: marker\n"),
    ):
        atomic_write(path, ctx.sops.encrypt(text, filename=path.name, public_key=public))
    return keyed_repo
