"""identity new, init."""

from __future__ import annotations

import getpass
import os
from pathlib import Path

import typer

from talkub.errors import ValidationError
from talkub.identity import create_identity, identities_dir, own_recipients
from talkub.repo import NodeSpec
from talkub.runner import Runner
from talkub.scaffold import InitRequest, init_cluster

identity_app = typer.Typer(help="Manage your personal age identities.", no_args_is_help=True)


@identity_app.command("new")
def identity_new(
    software: bool = typer.Option(False, "--software", help="Plain age key file (default)."),
    yubikey: bool = typer.Option(False, "--yubikey", help="YubiKey via age-plugin-yubikey."),
    se: bool = typer.Option(False, "--se", help="macOS Secure Enclave via age-plugin-se."),
    name: str = typer.Option(None, "--name", help="File name (default: the kind)."),
) -> None:
    """Create an identity in ~/.config/talkub/identities and print its public key."""
    chosen = [kind for kind, flag in (("software", software), ("yubikey", yubikey), ("se", se)) if flag]
    if len(chosen) > 1:
        raise ValidationError("choose only one of --software, --yubikey, --se")
    path, public = create_identity(Runner(), (chosen or ["software"])[0], identities_dir(os.environ), name)
    print(f"identity: {path}")
    print(f"public key: {public}")
    print("Share the public key with an admin: talkub admin add <key> --name <you>")


def _parse_node(value: str) -> tuple[str, NodeSpec]:
    name, sep, ip = value.partition("=")
    if not sep or not name or not ip:
        raise ValidationError(f"invalid --node {value!r}", hint="use name=ip, e.g. cp-01=10.0.0.10")
    return name, NodeSpec(ip=ip)


def init(
    name: str = typer.Argument(..., help="Cluster name; also the directory name."),
    endpoint: str = typer.Option(..., "--endpoint", help="Kubernetes API endpoint, e.g. https://10.0.0.10:6443"),
    node: list[str] = typer.Option(..., "--node", help="Control plane node as name=ip (repeatable)."),
    git_url: str = typer.Option(..., "--git-url", help="Git URL ArgoCD pulls this repo from."),
    directory: Path = typer.Option(None, "--dir", help="Target directory (default: ./<name>)."),
) -> None:
    """Create a new cluster repo with keys, Talos secrets and the GitOps skeleton."""
    nodes = dict(_parse_node(value) for value in node)
    request = InitRequest(
        target=(directory or Path.cwd() / name),
        name=name,
        endpoint=endpoint,
        git_url=git_url,
        nodes=nodes,
        recipients=own_recipients(os.environ, getpass.getuser()),
    )
    repo = init_cluster(request, Runner())
    print(f"created {repo.root}")
    print("next: add a second admin or backup key (talkub admin add), then git add/commit")
