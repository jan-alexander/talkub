"""tailscale enable, tailscale lockdown, tailscale operator enable."""

from __future__ import annotations

from pathlib import Path

import typer

from talkub import tailscale, tailscale_operator
from talkub.cli.output import print_notes
from talkub.cli.prompts import read_secret

app = typer.Typer(
    help="Tailscale as the only admin path to the Talos and Kubernetes APIs.",
    no_args_is_help=True,
)
NODE_CIDR = typer.Option(
    None,
    "--node-cidr",
    help="Node network for cluster-internal rules (default: /24 of the first node).",
)

operator_app = typer.Typer(
    help="The Tailscale Kubernetes operator (egress to tailnet hosts; ingress via `talkub tailscale expose`).",
    no_args_is_help=True,
)
app.add_typer(operator_app, name="operator")

egress_app = typer.Typer(
    help="Egress: reach tailnet hosts from pods via ExternalName Services.",
    no_args_is_help=True,
)
app.add_typer(egress_app, name="egress")


def read_authkey(path: Path | None, repo_root: Path | None = None) -> str:
    """From a file, or prompted without echo — never from the command line."""
    return read_secret(path, "Tailscale auth key: ", repo_root)


@app.command("enable")
def enable(
    ctx: typer.Context,
    tailnet: str = typer.Option(
        ..., "--tailnet", help="Tailnet DNS name, e.g. tail1234.ts.net."
    ),
    authkey_file: Path = typer.Option(
        None, "--authkey-file", help="File with the auth key (default: prompt)."
    ),
    node_cidr: str = NODE_CIDR,
) -> None:
    """Step 1: add the Tailscale extension and auth key (addresses stay on the LAN)."""
    context = ctx.obj.context()
    tailscale.validate_tailnet(tailnet)
    tailscale.check_can_enable(context.repo)
    authkey = read_authkey(authkey_file, repo_root=context.repo.root)
    print_notes(
        tailscale.enable(context, tailnet=tailnet, authkey=authkey, node_cidr=node_cidr)
    )


@app.command("lockdown")
def lockdown(
    ctx: typer.Context,
    node_cidr: str = NODE_CIDR,
    use_ips: bool = typer.Option(
        False,
        "--use-ips",
        help="Use the nodes' tailnet IPs instead of MagicDNS names (for clients without MagicDNS).",
    ),
) -> None:
    """Step 2: Talos firewall + tailnet addresses for talosctl/kubectl."""
    print_notes(
        tailscale.lockdown(ctx.obj.context(), node_cidr=node_cidr, use_ips=use_ips)
    )


@operator_app.command("enable")
def operator_enable(
    ctx: typer.Context,
    client_id: str = typer.Option(
        None, "--client-id", help="OAuth client ID (default: prompt)."
    ),
    client_secret_file: Path = typer.Option(
        None,
        "--client-secret-file",
        help="File with the OAuth client secret (default: prompt).",
    ),
    rotate_credentials: bool = typer.Option(
        False,
        "--rotate-credentials",
        help="Replace the OAuth client of an already-enabled operator.",
    ),
) -> None:
    """Install the Tailscale Kubernetes operator (Helm release, DNSConfig, sealed OAuth client)."""
    context = ctx.obj.context()
    tailscale_operator.check_can_enable(
        context.repo, rotate_credentials=rotate_credentials
    )
    if not client_id:
        client_id = typer.prompt("OAuth client ID")
    client_secret = read_secret(
        client_secret_file, "OAuth client secret: ", repo_root=context.repo.root
    )
    print_notes(
        tailscale_operator.enable(
            context,
            client_id=client_id,
            client_secret=client_secret,
            rotate_credentials=rotate_credentials,
        )
    )


@egress_app.command("add")
def egress_add(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Egress name; becomes the Service name."),
    fqdn: str = typer.Option(
        None, "--fqdn", help="Tailnet DNS name to reach, e.g. host.tail1234.ts.net."
    ),
    ip: str = typer.Option(
        None, "--ip", help="Tailnet IP to reach, instead of --fqdn."
    ),
    namespace: str = typer.Option(
        tailscale_operator.NAMESPACE,
        "--namespace",
        help="Namespace for the Service (must already exist if not 'tailscale').",
    ),
) -> None:
    """Write a Service that lets pods reach a tailnet host or address by name (`<name>.<namespace>.svc.cluster.local`)."""
    context = ctx.obj.context()
    tailscale_operator.egress_add(context, name, fqdn=fqdn, ip=ip, namespace=namespace)
    print(f"note: {tailscale_operator.EGRESS_COMMIT_NOTE}")


@egress_app.command("remove")
def egress_remove(
    ctx: typer.Context, name: str = typer.Argument(..., help="Egress name.")
) -> None:
    """Delete the egress Service manifest."""
    context = ctx.obj.context()
    tailscale_operator.egress_remove(context, name)
    print(f"note: {tailscale_operator.EGRESS_COMMIT_NOTE}")


@app.command("expose")
def expose(
    ctx: typer.Context,
    name: str = typer.Argument(
        ..., help="Ingress name; becomes the tailnet device name."
    ),
    service: str = typer.Option(
        ..., "--service", help="Backend as <namespace>/<service>."
    ),
    port: str = typer.Option(..., "--port", help="Backend port number or name."),
    path: str = typer.Option(
        None,
        "--path",
        help="Path-based rule (starting with '/') instead of a default backend.",
    ),
) -> None:
    """Expose a Service in the tailnet at https://<name>.<tailnet> via a Tailscale Ingress."""
    context = ctx.obj.context()
    tailscale_operator.expose(context, name, service=service, port=port, path=path)
    print_notes(
        [
            tailscale_operator.INGRESS_BACKEND_NOTE,
            tailscale_operator.INGRESS_CERT_NOTE,
            tailscale_operator.INGRESS_COMMIT_NOTE,
        ]
    )


@app.command("unexpose")
def unexpose(
    ctx: typer.Context, name: str = typer.Argument(..., help="Ingress name.")
) -> None:
    """Delete the Ingress manifest written by `expose`."""
    context = ctx.obj.context()
    tailscale_operator.unexpose(context, name)
    print(f"note: {tailscale_operator.INGRESS_COMMIT_NOTE}")
