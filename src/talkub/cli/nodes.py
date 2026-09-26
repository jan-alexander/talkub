"""node add."""

from __future__ import annotations

import sys

import typer

from talkub.config import NodeSpec
from talkub.nodes import NodeOptions, add_node, node_patch_path

app = typer.Typer(help="Describe cluster nodes.", no_args_is_help=True)


def node_options(
    static: str | None,
    gateway: str | None,
    mac: str | None,
    dns: list[str] | None,
    disk_selector: str | None,
    public_ingress: bool,
) -> NodeOptions:
    return NodeOptions(
        static=static,
        gateway=gateway,
        mac=mac,
        dns=tuple(dns or ()),
        disk_selector=disk_selector,
        public_ingress=public_ingress,
    )


STATIC = typer.Option(
    None,
    "--static",
    help="Static address with prefix, e.g. 10.0.0.10/24 (default: DHCP).",
)
GATEWAY = typer.Option(None, "--gateway", help="Default gateway (with --static).")
MAC = typer.Option(
    None, "--mac", help="MAC address of the interface to configure (with --static)."
)
DNS = typer.Option(None, "--dns", help="DNS server (repeatable).")
DISK = typer.Option(
    None,
    "--disk-selector",
    help='CEL install disk selector, e.g. disk.transport == "nvme".',
)
PUBLIC = typer.Option(
    False,
    "--public-ingress",
    help="Open 80/443 from anywhere (with the Talos firewall).",
)


def print_warnings(warnings: list[str]) -> None:
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)


@app.command("add")
def add(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Node name; becomes the hostname."),
    ip: str = typer.Option(
        ..., "--ip", help="Address for the first install (LAN or public)."
    ),
    role: str = typer.Option("controlplane", "--role", help="controlplane or worker."),
    platform: str = typer.Option(
        None, "--platform", help="metal or hcloud (default: cluster platform)."
    ),
    arch: str = typer.Option(
        None, "--arch", help="amd64 or arm64 (for boot media URLs)."
    ),
    mgmt: str = typer.Option(
        None, "--mgmt", help="Address used after installation, e.g. a Tailscale name."
    ),
    static: str = STATIC,
    gateway: str = GATEWAY,
    mac: str = MAC,
    dns: list[str] = DNS,
    disk_selector: str = DISK,
    public_ingress: bool = PUBLIC,
) -> None:
    """Add a node: writes talos/nodes/<name>.yaml and the entry in .talkub.yaml."""
    repo = ctx.obj.repo()
    spec = NodeSpec(ip=ip, role=role, mgmt=mgmt, platform=platform, arch=arch)
    options = node_options(static, gateway, mac, dns, disk_selector, public_ingress)
    print_warnings(add_node(repo, name, spec, options))
    print(f"added node {name}: {repo.rel(node_patch_path(repo, name))}")
    print(f"next: boot the node, then `talkub apply --insecure {name}`")
