"""tailscale enable, tailscale lockdown."""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

import typer

from talkub import tailscale
from talkub.errors import ValidationError

app = typer.Typer(help="Tailscale as the only admin path to the Talos and Kubernetes APIs.", no_args_is_help=True)
NODE_CIDR = typer.Option(None, "--node-cidr", help="Node network for cluster-internal rules (default: /24 of the first node).")


def read_authkey(path: Path | None, repo_root: Path | None = None) -> str:
    """From a file, or prompted without echo — never from the command line."""
    if path is not None:
        try:
            key = path.expanduser().read_text().strip()
        except OSError as err:
            raise ValidationError(f"cannot read {path}: {err.strerror}") from None
        if repo_root is not None and path.expanduser().resolve().is_relative_to(repo_root.resolve()):
            print(
                f"warning: {path} is inside the cluster repo; the plaintext auth key file must not be "
                "committed — delete it after use",
                file=sys.stderr,
            )
        return key
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
    tailscale.check_can_enable(context.repo)
    authkey = read_authkey(authkey_file, repo_root=context.repo.root)
    print_notes(tailscale.enable(context, tailnet=tailnet, authkey=authkey, node_cidr=node_cidr))


@app.command("lockdown")
def lockdown(ctx: typer.Context, node_cidr: str = NODE_CIDR) -> None:
    """Step 2: Talos firewall + tailnet addresses for talosctl/kubectl."""
    print_notes(tailscale.lockdown(ctx.obj.context(), node_cidr=node_cidr))
