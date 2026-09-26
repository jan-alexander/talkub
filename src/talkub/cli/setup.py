"""identity new, init."""

from __future__ import annotations

import getpass
import os
from pathlib import Path

import typer

from talkub import gitrepo, hooks, tailscale
from talkub.cli.nodes import (
    DISK,
    DNS,
    GATEWAY,
    MAC,
    PUBLIC,
    STATIC,
    node_options,
    print_warnings,
)
from talkub.cli.tailscale import print_notes, read_authkey
from talkub.context import Context
from talkub.errors import TalkubError, ValidationError
from talkub.identity import create_identity, identities_dir, own_recipients
from talkub.nodes import NodeOptions, static_address_warning, validate_address, validate_node_name
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
    validate_node_name(name)
    validate_address(ip, "--node")
    return name, NodeSpec(ip=ip)


def init(
    name: str = typer.Argument(..., help="Cluster name; also the directory name."),
    endpoint: str = typer.Option(..., "--endpoint", help="Kubernetes API endpoint, e.g. https://10.0.0.10:6443"),
    node: list[str] = typer.Option(..., "--node", help="Control plane node as name=ip (repeatable)."),
    git_url: str = typer.Option(..., "--git-url", help="Git URL ArgoCD pulls this repo from."),
    directory: Path = typer.Option(None, "--dir", help="Target directory (default: ./<name>)."),
    platform: str = typer.Option("metal", "--platform", help="Cluster platform: metal or hcloud."),
    static: str = STATIC,
    gateway: str = GATEWAY,
    mac: str = MAC,
    dns: list[str] = DNS,
    disk_selector: str = DISK,
    public_ingress: bool = PUBLIC,
    use_tailscale: bool = typer.Option(False, "--tailscale", help="Tailscale as the only admin path."),
    tailnet: str = typer.Option(None, "--tailnet", help="Tailnet DNS name (with --tailscale)."),
    tailscale_authkey_file: Path = typer.Option(None, "--tailscale-authkey-file", help="Auth key file (default: prompt)."),
    node_cidr: str = typer.Option(None, "--node-cidr", help="Node network for the firewall (with --tailscale)."),
) -> None:
    """Create a new cluster repo with keys, Talos secrets and the GitOps skeleton."""
    authkey = None
    if use_tailscale:
        if not tailnet:
            raise ValidationError("--tailscale needs --tailnet", hint="e.g. --tailnet tail1234.ts.net")
        tailscale.validate_tailnet(tailnet)
        if node_cidr:
            tailscale.validate_cidr(node_cidr)
        authkey = tailscale.validate_authkey(read_authkey(tailscale_authkey_file))
    elif tailnet or tailscale_authkey_file or node_cidr:
        raise ValidationError("--tailnet/--tailscale-authkey-file/--node-cidr need --tailscale")
    nodes = dict(_parse_node(value) for value in node)
    options = node_options(static, gateway, mac, dns, disk_selector, public_ingress)
    node_opts: dict[str, NodeOptions] = {}
    if options != NodeOptions():
        if len(nodes) != 1:
            raise ValidationError(
                "node options (--static, --mac, --dns, --disk-selector, --public-ingress) need exactly one --node",
                hint="add further nodes with `talkub node add`",
            )
        warnings = options.check()
        node_name, spec = next(iter(nodes.items()))
        moved = static_address_warning(spec, options, fix=f"set nodes.{node_name}.mgmt: {{address}} in .talkub.yaml")
        print_warnings(warnings + ([moved] if moved else []))
        node_opts = {node_name: options}
    request = InitRequest(
        target=(directory or Path.cwd() / name),
        name=name,
        endpoint=endpoint,
        git_url=git_url,
        nodes=nodes,
        recipients=own_recipients(os.environ, getpass.getuser()),
        platform=platform,
        node_options=node_opts,
    )
    repo = init_cluster(request, Runner())
    print_warnings(gitrepo.git_init(repo.root, git_url.strip(), Runner()))
    try:
        print_notes(hooks.install(repo, Runner()))
    except TalkubError as err:
        print_warnings([f"could not install the pre-commit hook: {err}"])
    if authkey is not None:
        context = Context.open(repo)
        try:
            print_notes(tailscale.enable(context, tailnet=tailnet, authkey=authkey, node_cidr=node_cidr))
        except TalkubError as err:
            cidr = f" --node-cidr {node_cidr}" if node_cidr else ""
            hint = f"cd {repo.root} && talkub tailscale enable --tailnet {tailnet}{cidr} && talkub tailscale lockdown"
            raise type(err)(
                f"created {repo.root}, but the Tailscale setup failed: {err}",
                hint=f"{hint}; {err.hint}" if err.hint else hint,
            ) from err
        try:
            print_notes(tailscale.lockdown(Context.open(repo)))
        except TalkubError as err:
            hint = f"cd {repo.root} && talkub tailscale lockdown"
            raise type(err)(
                f"created {repo.root}, but the Tailscale setup failed: {err}",
                hint=f"{hint}; {err.hint}" if err.hint else hint,
            ) from err
    print(f"created {repo.root}")
    print("next: add a second admin or backup key (talkub admin add), then git add -A && git commit")
