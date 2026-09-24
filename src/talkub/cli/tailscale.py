"""tailscale enable, tailscale lockdown."""

from __future__ import annotations

import getpass
from pathlib import Path

import typer

from talkub import tailscale
from talkub.errors import ValidationError

app = typer.Typer(help="Tailscale as the only admin path to the Talos and Kubernetes APIs.", no_args_is_help=True)
NODE_CIDR = typer.Option(None, "--node-cidr", help="Node network for cluster-internal rules (default: /24 of the first node).")


def read_authkey(path: Path | None) -> str:
    """From a file, or prompted without echo — never from the command line."""
    if path is not None:
        try:
            return path.expanduser().read_text().strip()
        except OSError as err:
            raise ValidationError(f"cannot read {path}: {err.strerror}") from None
    return getpass.getpass("Tailscale auth key: ")


def print_notes(notes: list[str]) -> None:
    for note in notes:
        print(f"note: {note}")


@app.command("enable")
def enable(
    ctx: typer.Context,
    tailnet: str = typer.Option(..., "--tailnet", help="Tailnet DNS name, e.g. tail1234.ts.net."),
    authkey_file: Path = typer.Option(None, "--authkey-file", help="File with the auth key (default: prompt)."),
    node_cidr: str = NODE_CIDR,
) -> None:
    """Step 1: add the Tailscale extension and auth key (addresses stay on the LAN)."""
    context = ctx.obj.context()
    tailscale.validate_tailnet(tailnet)
    print_notes(tailscale.enable(context, tailnet=tailnet, authkey=read_authkey(authkey_file), node_cidr=node_cidr))


@app.command("lockdown")
def lockdown(ctx: typer.Context, node_cidr: str = NODE_CIDR) -> None:
    """Step 2: Talos firewall + tailnet addresses for talosctl/kubectl."""
    print_notes(tailscale.lockdown(ctx.obj.context(), node_cidr=node_cidr))
